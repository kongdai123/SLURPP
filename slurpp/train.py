# this code is modified from Marigold: https://github.com/prs-eth/Marigold

import argparse
import logging
import os
from datetime import datetime, timedelta

import torch
from omegaconf import OmegaConf
from torch.utils.data import DataLoader

from slurpp.slurpp_pipeline import SlurppPipeline

from src.trainer import get_trainer_cls
from src.util.config_util import (
    recursive_load_config,
)

from src.util.logging_util import (
    config_logging,
    init_wandb,
    load_wandb_job_id,
    log_slurm_job_id,
    save_wandb_job_id,
    tb_logger,
)

import numpy as np
from src.util.myutils import *
from my_diffusers.dual_unet_condition import DualUNetCondition
from datasets.UR_revised_dataloader import UnderwaterDataset
from datasets.UR_real_data import UnderwaterRealDataset

if "__main__" == __name__:
    t_start = datetime.now()
    print(f"start at {t_start}")

    # -------------------- Arguments --------------------
    parser = argparse.ArgumentParser(description="Train your cute model!")
    parser.add_argument(
        "--config",
        type=str,
        default="",
        help="Path to config file.",
    )
    parser.add_argument(
        "--resume_run",
        action="store",
        default=None,
        help="Path of checkpoint to be resumed. If given, will ignore --config, and checkpoint in the config",
    )
    parser.add_argument(
        "--output_dir", type=str, default=None, help="directory to save checkpoints"
    )
    parser.add_argument("--no_cuda", action="store_true", help="Do not use cuda.")
    parser.add_argument(
        "--exit_after",
        type=int,
        default=-1,
        help="Save checkpoint and exit after X minutes.",
    )
    parser.add_argument("--no_wandb", action="store_true", help="run without wandb")
    parser.add_argument(
        "--do_not_copy_data",
        action="store_true",
        help="On Slurm cluster, do not copy data to local scratch",
    )
    parser.add_argument(
        "--base_data_dir", type=str, default=None, help="directory of training data"
    )
    parser.add_argument(
        "--base_ckpt_dir",
        type=str,
        default=None,
        help="directory of pretrained checkpoint",
    )
    parser.add_argument(
        "--add_datetime_prefix",
        action="store_true",
        help="Add datetime to the output folder name",
    )

    parser.add_argument("--job_name_prefix", default='', help="xmy or wtf")
    
    

    args = parser.parse_args()
    resume_run = args.resume_run
    output_dir = args.output_dir

    base_ckpt_dir = (
        args.base_ckpt_dir
        if args.base_ckpt_dir is not None
        else os.environ["BASE_CKPT_DIR"]
    )

    # -------------------- Initialization --------------------


    # Resume previous run
    if resume_run is not None:
        print(f"Resume run: {resume_run}")
        out_dir_run = os.path.dirname(os.path.dirname(resume_run))
        job_name = os.path.basename(out_dir_run)
        # Resume config file
        cfg = OmegaConf.load(os.path.join(out_dir_run, "config.yaml"))
    else:
        # Run from start
        print(f"loading config from {args.config}")
        cfg = recursive_load_config(args.config)
        # Full job name
        pure_job_name = getattr(cfg, 'job_name', os.path.basename(args.config).split(".")[0]) 
        # Add time prefix
        if args.add_datetime_prefix:
            job_name = f"{pure_job_name}_EBS{cfg.dataloader.effective_batch_size}_{t_start.strftime('%m%d_%H_%M_%S')}"
        else:
            job_name = pure_job_name

        job_name = f"{args.job_name_prefix}{job_name}"

        # Output dir
        if output_dir is not None:
            out_dir_run = os.path.join(output_dir, job_name)
        else:
            out_dir_run = os.path.join("./output", job_name)
        os.makedirs(out_dir_run, exist_ok=False)

    # cfg_data = cfg.dataset
    if cfg.dataloader.effective_batch_size < cfg.dataloader.max_train_batch_size:
        cfg.dataloader.effective_batch_size = cfg.dataloader.max_train_batch_size


    # Other directories
    out_dir_ckpt = os.path.join(out_dir_run, "checkpoint")
    if not os.path.exists(out_dir_ckpt):
        os.makedirs(out_dir_ckpt)
    out_dir_tb = os.path.join(out_dir_run, "tensorboard")
    if not os.path.exists(out_dir_tb):
        os.makedirs(out_dir_tb)
    out_dir_eval = os.path.join(out_dir_run, "evaluation")
    if not os.path.exists(out_dir_eval):
        os.makedirs(out_dir_eval)
    out_dir_vis = os.path.join(out_dir_run, "visualization")
    if not os.path.exists(out_dir_vis):
        os.makedirs(out_dir_vis)

    # -------------------- Logging settings --------------------
    config_logging(cfg.logging, out_dir=out_dir_run)
    logging.debug(f"config: {cfg}")

    # Initialize wandb
    if not args.no_wandb:
        if resume_run is not None:
            wandb_id = load_wandb_job_id(out_dir_run)
            wandb_cfg_dic = {
                "id": wandb_id,
                "resume": "must",
                **cfg.wandb,
            }
        else:
            wandb_cfg_dic = {
                "config": dict(cfg),
                "name": job_name,
                "mode": "online",
                **cfg.wandb,
            }
        wandb_cfg_dic.update({"dir": out_dir_run})
        wandb_run = init_wandb(enable=True, **wandb_cfg_dic)
        save_wandb_job_id(wandb_run, out_dir_run)
    else:
        init_wandb(enable=False)

    # Tensorboard (should be initialized after wandb)
    tb_logger.set_dir(out_dir_tb)

    log_slurm_job_id(step=0)

    # -------------------- Device --------------------
    cuda_avail = torch.cuda.is_available() and not args.no_cuda
    device = torch.device("cuda" if cuda_avail else "cpu")
    logging.info(f"device = {device}")

    # -------------------- Snapshot of code and config --------------------
    if resume_run is None:
        _output_path = os.path.join(out_dir_run, "config.yaml")
        with open(_output_path, "w+") as f:
            OmegaConf.save(config=cfg, f=f)
        logging.info(f"Config saved to {_output_path}")
        # Copy and tar code on the first run
        _temp_code_dir = os.path.join(out_dir_run, "code_tar")
        _code_snapshot_path = os.path.join(out_dir_run, "code_snapshot.tar")
        os.system(
            f"rsync --relative -arhvz --quiet --filter=':- .gitignore' --exclude '.git' . '{_temp_code_dir}'"
        )
        os.system(f"tar -cf {_code_snapshot_path} {_temp_code_dir}")
        os.system(f"rm -rf {_temp_code_dir}")
        logging.info(f"Code snapshot saved to: {_code_snapshot_path}")



    # -------------------- Gradient accumulation steps --------------------
    eff_bs = cfg.dataloader.effective_batch_size
    accumulation_steps = eff_bs / cfg.dataloader.max_train_batch_size
    assert int(accumulation_steps) == accumulation_steps
    accumulation_steps = int(accumulation_steps)

    logging.info(
        f"Effective batch size: {eff_bs}, accumulation steps: {accumulation_steps}"
    )

    # -------------------- Data --------------------

    image_size = getattr(cfg.dataloader, 'image_size', 256)
    print(f"image_size: {image_size}")

    torch.manual_seed(42)
    np.random.seed(42)

    val_size = cfg.dataloader.val_size
    train_vis_size = cfg.dataloader.train_vis_size
    


    train_ds = UnderwaterDataset(image_size=image_size)
    print(f"train_ds: {len(train_ds)}")
    train_inds = np.arange(len(train_ds))
    np.random.shuffle(train_inds)
    train_vis_inds = train_inds[:train_vis_size]
    train_vis_ds = torch.utils.data.Subset(train_ds, train_vis_inds)

    real_vis_ds = UnderwaterRealDataset(image_size=image_size)
    real_vis_inds = np.arange(len(real_vis_ds))
    np.random.shuffle(real_vis_inds)
    real_vis_inds = real_vis_inds[:train_vis_size]
    real_vis_ds = torch.utils.data.Subset(real_vis_ds, real_vis_inds)
    

    train_dataloader = DataLoader(train_ds, 
                                  batch_size=cfg.dataloader.max_train_batch_size,
                                  num_workers=cfg.dataloader.num_workers,
                                  shuffle=True, 
                                  pin_memory=True, 
                                  persistent_workers=(cfg.dataloader.num_workers > 0))
    val_dataloader = DataLoader(train_vis_ds, 
                                num_workers=cfg.dataloader.num_workers, 
                                batch_size=1, 
                                shuffle=False)
    
    train_vis_dataloader = DataLoader(train_vis_ds,
                                        num_workers=cfg.dataloader.num_workers,
                                        batch_size=1,
                                        shuffle=False)

    real_vis_dataloader = DataLoader(real_vis_ds,
                                        num_workers=cfg.dataloader.num_workers,
                                        batch_size=1,
                                        shuffle=False)

    # -------------------- Model ---------
    # -----------
    model_path = os.path.join(base_ckpt_dir, cfg.model.pretrained_path)
    print(f"loading model from {model_path}")
    model = SlurppPipeline.from_pretrained(
        model_path
    )

    dual = getattr(cfg, "dual", False)

    if dual:
        print("Initialize dual model")
        model_path2= os.path.join(base_ckpt_dir, cfg.model.pretrained_path2)
        model.unet = DualUNetCondition(unet_path1 = model_path, unet_path2 = model_path2)   
        print(f"loading dual model from {model_path2}")

        
        # -------------------- Trainer --------------------
    # Exit time
    if args.exit_after > 0:
        t_end = t_start + timedelta(minutes=args.exit_after)
        logging.info(f"Will exit at {t_end}")
    else:
        t_end = None

    trainer_cls = get_trainer_cls(cfg.trainer.name)
    logging.debug(f"Trainer: {trainer_cls}")
    trainer = trainer_cls(
        cfg=cfg,
        model=model,
        train_dataloader=train_dataloader,
        device=device,
        base_ckpt_dir=base_ckpt_dir,
        out_dir_ckpt=out_dir_ckpt,
        out_dir_eval=out_dir_eval,
        out_dir_vis=out_dir_vis,
        accumulation_steps=accumulation_steps,
        val_dataloaders=[val_dataloader],
        vis_dataloaders=[train_vis_dataloader],
        real_vis_dataloaders=[real_vis_dataloader],
    )

    # -------------------- Checkpoint --------------------

    fine_tune  = getattr(cfg, 'fine_tune', None)
    if fine_tune:
        print(f"Fine tune from {fine_tune}")
        trainer.load_checkpoint(fine_tune, load_trainer_state=False, resume_lr_scheduler=False)
    if resume_run is not None:
        trainer.load_checkpoint(
            resume_run, load_trainer_state=True, resume_lr_scheduler=True
        )

    # -------------------- Training & Evaluation Loop --------------------
    try:
        trainer.train(t_end=t_end)
    except Exception as e:
        logging.exception(e)
