import torch
import torch.nn as nn
from .utils import calc_gp, weights_init, save_model, load_model
from datasets.dataset import Dataset
from preprocessor import (
    MelNormalizer,
    MelDeNormalizer,
    FrameNormalizer,
    FrameDeNormalizer,
)
import torchvision
from torch.utils.data import DataLoader
from models.sound2image import Generator, Discriminator
from copy import copy
import os
from itertools import chain


DATA_CONFIG = {
    "load_files": ["frame", "log_mel_spec", "mel_if"],
    "mel_normalizer_savefile": "./normalizer/mel_normalizer.json",
    "normalizer_dir": "./normalizer",
    "D_checkpoint_dir": "./check_points/Discriminator",
    "G_checkpoint_dir": "./check_points/Generator",
    "test_output_dir": "./test_outputs",
}

MODEL_CONFIG = {
    "lr": 0.0001,
    "beta1": 0,
    "beta2": 0.99,
    "iters": 100,
    "print_epoch": 100,
    "test_epoch": 500,
    "save_per": 1,
    "fm_lamba": 0,
}


def train(data_dir, test_data_dir, config={}, exp_dir="./experiments", device="cuda"):
    # refine path with exp_dir
    data_config = copy(DATA_CONFIG)
    model_config = copy(MODEL_CONFIG)
    model_config = {**model_config, **config}

    for key in [
        "mel_normalizer_savefile",
        "normalizer_dir",
        "D_checkpoint_dir",
        "G_checkpoint_dir",
        "test_output_dir",
    ]:
        data_config[key] = os.path.join(exp_dir, data_config[key])

        if "_dir" in key:
            os.makedirs(data_config[key], exist_ok=True)

    # for normalizer of mel
    mel_data_loader = None
    if not os.path.isfile(data_config["mel_normalizer_savefile"]):
        mel_dataset = Dataset(
            data_dir=data_dir, transforms={}, load_files=["log_mel_spec", "mel_if"]
        )
        mel_data_loader = DataLoader(
            dataset=mel_dataset,
            batch_size=model_config["batch_size"],
            shuffle=False,
            pin_memory=True,
            num_workers=model_config["batch_size"] // 2,
        )

    # Data definitions
    transforms = {
        "frame": torchvision.transforms.Compose(
            [
                torchvision.transforms.ToPILImage(),
                torchvision.transforms.RandomVerticalFlip(p=0.5),
                torchvision.transforms.ToTensor(),
                torchvision.transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
            ]
        ),
        "mel": MelNormalizer(
            dataloader=mel_data_loader,
            savefile_path=data_config["mel_normalizer_savefile"],
        ),
    }

    # Define train_data loader
    dataset = Dataset(
        data_dir=data_dir, transforms=transforms, load_files=data_config["load_files"]
    )
    data_loader = DataLoader(
        dataset=dataset,
        batch_size=model_config["batch_size"],
        shuffle=True,
        pin_memory=True,
        num_workers=model_config["batch_size"] // 2,
    )

    # Define train_data loader
    test_dataset = Dataset(
        data_dir=test_data_dir,
        transforms=transforms,
        load_files=data_config["load_files"],
    )
    test_data_loader = DataLoader(
        dataset=test_dataset,
        batch_size=model_config["batch_size"],
        shuffle=True,
        pin_memory=True,
        num_workers=model_config["batch_size"] // 2,
    )
    test_data_loader_ = iter(test_data_loader)

    # model definition
    netG = Generator()
    netD = Discriminator()

    # weight initialize
    netG.apply(weights_init)
    netD.apply(weights_init)

    # parallelize of model
    if torch.cuda.device_count() > 1:
        print("NOTICE: use ", torch.cuda.device_count(), "GPUs")
        netG = nn.DataParallel(netG)
        netD = nn.DataParallel(netD)
    else:
        print(f"NOTICE: use {device}")

    # load model
    g_last_iter = load_model(model=netG, dir=data_config["G_checkpoint_dir"])
    d_last_iter = load_model(model=netD, dir=data_config["D_checkpoint_dir"])

    last_iter = min(g_last_iter, d_last_iter)
    if g_last_iter != last_iter:
        load_model(model=netG, dir=data_config["G_checkpoint_dir"], load_iter=last_iter)

    if d_last_iter != last_iter:
        load_model(model=netD, dir=data_config["D_checkpoint_dir"], load_iter=last_iter)

    # model to device
    netG.to(device)
    netD.to(device)

    # set optimizer
    optimizer_d = torch.optim.Adam(
        netD.parameters(),
        model_config["lr"],
        (model_config["beta1"], model_config["beta2"]),
    )
    optimizer_g = torch.optim.Adam(
        netG.parameters(),
        model_config["lr"],
        (model_config["beta1"], model_config["beta2"]),
    )

    for iter_ in range(model_config["iters"]):
        if iter_ <= last_iter:
            continue

        for idx, data_dict in enumerate(data_loader):
            data_dict = {key: value.to(device) for key, value in data_dict.items()}
            mel_data = torch.stack(
                [data_dict["log_mel_spec"], data_dict["mel_if"]], dim=1
            )

            # TRAIN MODE
            netD.train()
            netG.train()

            ############
            # Update D #
            ############
            netD.zero_grad()
            netG.zero_grad()

            feature_real, D_real_out = netD(data_dict["frame"])
            feature_real = feature_real.detach()
            D_real = D_real_out.view(-1).mean()

            gen_frames = netG(mel_data)
            _, D_fake_out = netD(gen_frames.detach())
            D_fake = D_fake_out.view(-1).mean()

            gp = calc_gp(
                discriminator=netD,
                real_images=data_dict["frame"],
                fake_images=gen_frames.detach(),
                device=device,
            )

            wasserstein_D = D_real - D_fake
            d_loss = D_fake - D_real + gp
            d_loss.backward()

            optimizer_d.step()

            ############
            # Update G #
            ############
            netD.zero_grad()
            netG.zero_grad()

            feature_fake, DG_fake_out = netD(gen_frames)
            DG_fake = DG_fake_out.view(-1).mean()
            g_loss = (
                (feature_real - feature_fake).view(-1).mean() * model_config["fm_lamba"]
            ) - DG_fake
            g_loss.backward()

            optimizer_g.step()

            if idx % model_config["print_epoch"] == 0:
                print(
                    f"INFO: D_loss: {d_loss.item():4f} | G_loss: {g_loss.item():4f} | W_D: {wasserstein_D.item():4f}"
                )

            if idx % model_config["test_epoch"] == 0:
                # EVAL MODE
                netD.eval()
                netG.eval()

                try:
                    test_data_dict = next(test_data_loader_)
                except StopIteration:
                    test_data_loader_ = iter(test_data_loader)
                    test_data_dict = next(test_data_loader_)

                test_data_dict = dict(
                    [
                        (key, value.to(device))
                        if key in ["log_mel_spec", "mel_if"]
                        else (key, value.to("cpu"))
                        for key, value in test_data_dict.items()
                    ]
                )

                mel_test_data = torch.stack(
                    [test_data_dict["log_mel_spec"], test_data_dict["mel_if"]], dim=1
                )

                with torch.no_grad():
                    gen_frames = netG(mel_test_data).detach()

                concat_frames = torch.stack(
                    list(
                        chain(
                            *[
                                (fake_img, real_img)
                                for fake_img, real_img in zip(
                                    gen_frames.cpu(), test_data_dict["frame"]
                                )
                            ]
                        )
                    ),
                    dim=0,
                )
                concat_frames = torchvision.utils.make_grid(
                    concat_frames, nrow=2, padding=10
                )

                torchvision.utils.save_image(
                    concat_frames,
                    os.path.join(data_config["test_output_dir"], f"{iter_}-{idx}.png"),
                )

        if iter_ % model_config["save_per"] == 0:
            save_model(model=netG, dir=data_config["G_checkpoint_dir"], iter=iter_)
            save_model(model=netD, dir=data_config["D_checkpoint_dir"], iter=iter_)
