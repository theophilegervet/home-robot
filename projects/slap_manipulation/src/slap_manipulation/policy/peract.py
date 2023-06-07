import copy
import glob
import os

# import pickle
# import shutil
# import sys
import time

import click
import clip

# import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import open3d as o3d
import slap_manipulation.policy.peract_utils as utils
import torch
import torch.nn.functional as F
import trimesh.transformations as tra
import yaml
from einops import rearrange, repeat  # , reduce
from einops.layers.torch import Reduce
from perceiver_pytorch.perceiver_io import PreNorm  # exists,
from perceiver_pytorch.perceiver_io import FeedForward, cache_fn, default
from slap_manipulation.dataloaders.peract_loader import PerActRobotDataset
from slap_manipulation.optim.lamb import Lamb
from slap_manipulation.policy.components import Attention

# from arm.demo_loading_utils import keypoint_discovery
from slap_manipulation.policy.peract_components import (  # Conv3DInceptionBlock,; Conv3DInceptionBlockUpsampleBlock,
    Conv3DBlock,
    Conv3DUpsampleBlock,
    DenseBlock,
    SpatialSoftmax3D,
)
from slap_manipulation.policy.peract_utils import (  # get_gripper_render_pose,
    discrete_euler_to_quaternion,
    visualise_voxel,
)
from slap_manipulation.policy.voxel_grid import VoxelGrid
from torch import nn
from tqdm import tqdm

from home_robot.utils.point_cloud import numpy_to_pcd

# from functools import reduce as funtool_reduce
# from functools import wraps
# from math import log, pi
# from operator import mul
# from typing import List


# constants
CAMERAS = ["front", "left_shoulder", "right_shoulder", "wrist"]
IMAGE_SIZE = 128  # 128x128 - if you want to use higher voxel resolutions like 200^3, you might want to regenerate the dataset with larger images

# settings
VOXEL_SIZES = [100]  # 100x100x100 voxels
# NUM_LATENTS = 512  # PerceiverIO latents
NUM_LATENTS = 256  # PerceiverIO latents
SCENE_BOUNDS = [
    -0.3,
    -0.5,
    0.6,
    0.7,
    0.5,
    1.6,
]  # [x_min, y_min, z_min, x_max, y_max, z_max] - the metric volume to be voxelized
BATCH_SIZE = 1


# Main PerceiverIO implementation, uses Attention block defined above
# PerceiverIO adapted for 6-DoF manipulation
class PerceiverIO(nn.Module):
    def __init__(
        self,
        depth,  # number of self-attention layers
        # number cross-attention iterations (PerceiverIO uses just 1)
        iterations,
        voxel_size,  # N voxels per side (size: N*N*N)
        initial_dim,  # 10 dimensions - dimension of the input sequence to be encoded
        # 4 dimensions - proprioception: {gripper_open, left_finger_joint, right_finger_joint, timestep}
        low_dim_size,
        layer=0,
        # 5 degree increments (5*72=360) for each of the 3-axis
        num_rotation_classes=72,
        num_grip_classes=2,  # open or not open
        num_collision_classes=2,  # collisions allowed or not allowed
        input_axis=3,  # 3D tensors have 3 axes
        num_latents=512,  # number of latent vectors
        im_channels=64,  # intermediate channel size
        latent_dim=512,  # dimensions of latent vectors
        cross_heads=1,  # number of cross-attention heads
        latent_heads=8,  # number of latent heads
        cross_dim_head=64,
        latent_dim_head=64,
        activation="relu",
        weight_tie_layers=False,
        input_dropout=0.1,
        attn_dropout=0.1,
        decoder_dropout=0.0,
        voxel_patch_size=5,  # intial patch size
        voxel_patch_stride=5,  # initial stride to patchify voxel input
        final_dim=64,  # final dimensions of features
    ):
        super().__init__()
        self.depth = depth
        self.layer = layer
        self.init_dim = int(initial_dim)
        self.iterations = iterations
        self.input_axis = input_axis
        self.voxel_size = voxel_size
        self.low_dim_size = low_dim_size
        self.im_channels = im_channels
        self.voxel_patch_size = voxel_patch_size
        self.voxel_patch_stride = voxel_patch_stride
        self.num_rotation_classes = num_rotation_classes
        self.num_grip_classes = num_grip_classes
        self.num_collision_classes = num_collision_classes
        self.final_dim = final_dim
        self.input_dropout = input_dropout
        self.attn_dropout = attn_dropout
        self.decoder_dropout = decoder_dropout

        # patchified input dimensions
        spatial_size = voxel_size // self.voxel_patch_stride  # 100/5 = 20

        # 64 voxel features + 64 proprio features
        self.input_dim_before_seq = self.im_channels * 2

        # learnable positional encoding
        lang_emb_dim, lang_max_seq_len = 512, 77
        self.pos_encoding = nn.Parameter(
            torch.randn(
                1, lang_max_seq_len + spatial_size**3, self.input_dim_before_seq
            )
        )

        # voxel input preprocessing encoder
        self.input_preprocess = Conv3DBlock(
            self.init_dim,
            self.im_channels,
            kernel_sizes=1,
            strides=1,
            norm=None,
            activation=activation,
        )

        # proprio preprocessing encoder
        self.proprio_preprocess = DenseBlock(
            self.low_dim_size,
            self.im_channels,
            norm=None,
            activation=activation,
        )

        # patchify conv
        self.patchify = Conv3DBlock(
            self.input_preprocess.out_channels,
            self.im_channels,
            kernel_sizes=self.voxel_patch_size,
            strides=self.voxel_patch_stride,
            norm=None,
            activation=activation,
        )

        # lang preprocess
        self.lang_preprocess = nn.Linear(lang_emb_dim, self.im_channels * 2)

        # pooling functions
        self.local_maxp = nn.MaxPool3d(3, 2, padding=1)
        self.global_maxp = nn.AdaptiveMaxPool3d(1)

        # 1st 3D softmax
        self.ss0 = SpatialSoftmax3D(
            self.voxel_size, self.voxel_size, self.voxel_size, self.im_channels
        )
        flat_size = self.im_channels * 4

        # latent vectors (that are randomly initialized)
        self.latents = nn.Parameter(torch.randn(num_latents, latent_dim))

        # encoder cross attention
        self.cross_attend_blocks = nn.ModuleList(
            [
                PreNorm(
                    latent_dim,
                    Attention(
                        latent_dim,
                        self.input_dim_before_seq,
                        heads=cross_heads,
                        dim_head=cross_dim_head,
                        dropout=input_dropout,
                    ),
                    context_dim=self.input_dim_before_seq,
                ),
                PreNorm(latent_dim, FeedForward(latent_dim)),
            ]
        )

        def get_latent_attn():
            return PreNorm(
                latent_dim,
                Attention(
                    latent_dim,
                    heads=latent_heads,
                    dim_head=latent_dim_head,
                    dropout=attn_dropout,
                ),
            )

        def get_latent_ff():
            return PreNorm(latent_dim, FeedForward(latent_dim))

        get_latent_attn, get_latent_ff = map(cache_fn, (get_latent_attn, get_latent_ff))

        # self-attention layers
        self.layers = nn.ModuleList([])
        cache_args = {"_cache": weight_tie_layers}

        for i in range(depth):
            self.layers.append(
                nn.ModuleList(
                    [get_latent_attn(**cache_args), get_latent_ff(**cache_args)]
                )
            )

        # decoder cross attention
        self.decoder_cross_attn = PreNorm(
            self.input_dim_before_seq,
            Attention(
                self.input_dim_before_seq,
                latent_dim,
                heads=cross_heads,
                dim_head=cross_dim_head,
                dropout=decoder_dropout,
            ),
            context_dim=latent_dim,
        )

        # upsample conv
        self.up0 = Conv3DUpsampleBlock(
            self.input_dim_before_seq,
            self.final_dim,
            kernel_sizes=self.voxel_patch_size,
            strides=self.voxel_patch_stride,
            norm=None,
            activation=activation,
        )

        # 2nd 3D softmax
        self.ss1 = SpatialSoftmax3D(
            spatial_size, spatial_size, spatial_size, self.input_dim_before_seq
        )

        flat_size += self.input_dim_before_seq * 4

        # final layers
        self.final = Conv3DBlock(
            self.im_channels * 2,
            self.im_channels,
            kernel_sizes=3,
            strides=1,
            norm=None,
            activation=activation,
        )

        # 100x100x100x64 -> 100x100x100x1 decoder for translation Q-values
        self.trans_decoder = Conv3DBlock(
            self.final_dim,
            1,
            kernel_sizes=3,
            strides=1,
            norm=None,
            activation=None,
        )

        # final 3D softmax
        self.ss_final = SpatialSoftmax3D(
            self.voxel_size, self.voxel_size, self.voxel_size, self.im_channels
        )

        flat_size += self.im_channels * 4

        # MLP layers
        self.dense0 = DenseBlock(flat_size, 256, None, activation)
        self.dense1 = DenseBlock(256, self.final_dim, None, activation)

        # 1x64 -> 1x(72+72+72+2+2) decoders for rotation, gripper open, and collision Q-values
        self.rot_grip_collision_ff = DenseBlock(
            self.final_dim,
            self.num_rotation_classes * 3
            + self.num_grip_classes
            + self.num_collision_classes,
            None,
            None,
        )

    def forward(
        self,
        ins,
        proprio,
        lang_goal_embs,
        bounds,
        mask=None,
    ):
        # preprocess
        # [B,10,100,100,100] -> [B,64,100,100,100]
        d0 = self.input_preprocess(ins)

        # aggregated features from 1st softmax and maxpool for MLP decoders
        feats = [self.ss0(d0.contiguous()), self.global_maxp(d0).view(ins.shape[0], -1)]

        # patchify input (5x5x5 patches)
        ins = self.patchify(d0)  # [B,64,100,100,100] -> [B,64,20,20,20]

        b, c, d, h, w, device = *ins.shape, ins.device
        axis = [d, h, w]
        assert (
            len(axis) == self.input_axis
        ), "input must have the same number of axis as input_axis"

        # concat proprio
        # This is a dense block - add to the set of inputs
        p = self.proprio_preprocess(proprio)  # [B,4] -> [B,64]
        p = p.unsqueeze(-1).unsqueeze(-1).unsqueeze(-1).repeat(1, 1, d, h, w)
        ins = torch.cat([ins, p], dim=1)  # [B,128,20,20,20]

        # channel last
        ins = rearrange(ins, "b d ... -> b ... d")  # [B,20,20,20,128]

        # save original shape of input for layer
        ins_orig_shape = ins.shape

        # flatten voxel grid into sequence
        ins = rearrange(ins, "b ... d -> b (...) d")  # [B,8000,128]

        # append language features as sequence
        lang_proc = self.lang_preprocess(lang_goal_embs)  # [B,77,1024] -> [B,77,128]
        ins = torch.cat((lang_proc, ins), dim=1)  # [B,8077,128]

        # add learnable pos encoding
        ins = ins + self.pos_encoding

        # batchify latents
        x = repeat(self.latents, "n d -> b n d", b=b)

        cross_attn, cross_ff = self.cross_attend_blocks

        for it in range(self.iterations):
            # encoder cross attention
            x = cross_attn(x, context=ins, mask=mask) + x
            x = cross_ff(x) + x

            # self-attention layers
            for self_attn, self_ff in self.layers:
                x = self_attn(x) + x
                x = self_ff(x) + x

        # decoder cross attention
        latents = self.decoder_cross_attn(ins, context=x)
        latents = latents[:, lang_proc.shape[1] :]

        # reshape back to voxel grid
        latents = latents.view(
            b, *ins_orig_shape[1:-1], latents.shape[-1]
        )  # [B,20,20,20,64]
        latents = rearrange(latents, "b ... d -> b d ...")  # [B,64,20,20,20]

        # aggregated features from 2nd softmax and maxpool for MLP decoders
        feats.extend(
            [self.ss1(latents.contiguous()), self.global_maxp(latents).view(b, -1)]
        )

        # upsample layer
        u0 = self.up0(latents)  # [B,64,100,100,100]

        # skip connection like in UNets
        u = self.final(
            torch.cat([d0, u0], dim=1)
        )  # [B,64+64,100,100,100] -> [B,64,100,100,100]

        # translation decoder
        # [B,64,100,100,100] -> [B,1,100,100,100]
        trans = self.trans_decoder(u)

        # aggregated features from final softmax and maxpool for MLP decoders
        feats.extend([self.ss_final(u.contiguous()), self.global_maxp(u).view(b, -1)])

        # decoder MLP layers for rotation, gripper open, and collision
        dense0 = self.dense0(torch.cat(feats, dim=1))
        dense1 = self.dense1(dense0)  # [B,72*3+2+2]

        # format output
        rot_and_grip_collision_out = self.rot_grip_collision_ff(dense1)
        rot_and_grip_out = rot_and_grip_collision_out[:, : -self.num_collision_classes]
        collision_out = rot_and_grip_collision_out[:, -self.num_collision_classes :]

        return trans, rot_and_grip_out, collision_out


# Q-Attention which builds on PerceiverIO for 6DOF manip
class QFunction(nn.Module):
    def __init__(
        self,
        perceiver_encoder: nn.Module,
        voxel_grid: VoxelGrid,
        rotation_resolution: float,
        device,
        training,
    ):
        super(QFunction, self).__init__()
        self._rotation_resolution = rotation_resolution
        self._voxel_grid = voxel_grid
        self._qnet = copy.deepcopy(perceiver_encoder)
        self._qnet._dev = device

    def _argmax_3d(self, tensor_orig):
        b, c, d, h, w = tensor_orig.shape  # c will be one
        idxs = tensor_orig.view(b, c, -1).argmax(-1)
        indices = torch.cat([((idxs // h) // d), (idxs // h) % w, idxs % w], 1)
        return indices

    def choose_highest_action(self, q_trans, q_rot_grip, q_collision):
        coords = self._argmax_3d(q_trans)
        rot_and_grip_indicies = None
        if q_rot_grip is not None:
            q_rot = torch.stack(
                torch.split(
                    q_rot_grip[:, :-2], int(360 // self._rotation_resolution), dim=1
                ),
                dim=1,
            )
            rot_and_grip_indicies = torch.cat(
                [
                    q_rot[:, 0:1].argmax(-1),
                    q_rot[:, 1:2].argmax(-1),
                    q_rot[:, 2:3].argmax(-1),
                    q_rot_grip[:, -2:].argmax(-1, keepdim=True),
                ],
                -1,
            )
            ignore_collision = q_collision[:, -2:].argmax(-1, keepdim=True)
        return coords, rot_and_grip_indicies, ignore_collision

    def forward(
        self, obs, proprio, pcd, lang_goal_embs, bounds: torch.Tensor = torch.Tensor()
    ):
        """Forward the q function.
        This is where we will actually compute the value of each action.
        """

        bs = obs.shape[0]
        # voxelize
        # Create the voxelization code here
        # Normalized RGB in flat_imag_features
        voxel_grid = self._voxel_grid.coords_to_bounding_voxel_grid(
            pcd, coord_features=obs, coord_bounds=bounds
        )
        # So this is a (1 x 100 x 100 x 100 x 10) grid
        # We are essentially binning everything into 1cm cubes
        # What is each value?
        # RGB features
        # 6 is voxel x
        # 7 is voxel y
        # 8 is voxel z
        # Occupancy

        # swap to channels first
        voxel_grid = voxel_grid.permute(0, 4, 1, 2, 3).detach()

        # batch bounds if necessary
        if bounds.shape[0] != bs:
            bounds = bounds.repeat(bs, 1)

        # forward pass
        q_trans, rot_and_grip_q, collision_q = self._qnet(
            voxel_grid, proprio, lang_goal_embs, bounds
        )
        return q_trans, rot_and_grip_q, collision_q, voxel_grid

    def latents(self):
        return self._qnet.latent_dict


# Main trainable actor class built on perceiverio + Q-attention mechanisms
class PerceiverActorAgent:
    def __init__(
        self,
        coordinate_bounds: list,
        perceiver_encoder: nn.Module,
        camera_names: list,
        batch_size: int,
        voxel_size: int,
        voxel_feature_size: int,
        num_rotation_classes: int,
        rotation_resolution: float,
        lr: float = 0.0001,
        image_resolution: list = None,
        lambda_weight_l2: float = 0.0,
        transform_augmentation: bool = True,
        transform_augmentation_xyz: list = [0.0, 0.0, 0.0],
        transform_augmentation_rpy: list = [0.0, 0.0, 180.0],
        transform_augmentation_rot_resolution: int = 5,
        optimizer_type: str = "lamb",
        num_pts=8000,
    ):
        self._coordinate_bounds = coordinate_bounds
        self._perceiver_encoder = perceiver_encoder
        self._camera_names = camera_names
        self._batch_size = batch_size
        self._voxel_size = voxel_size
        self._voxel_feature_size = voxel_feature_size
        self._num_rotation_classes = num_rotation_classes
        self._rotation_resolution = rotation_resolution
        self._lr = lr
        self._image_resolution = image_resolution
        self._lambda_weight_l2 = lambda_weight_l2
        self._transform_augmentation = transform_augmentation
        self._transform_augmentation_xyz = transform_augmentation_xyz
        self._transform_augmentation_rpy = transform_augmentation_rpy
        self._transform_augmentation_rot_resolution = (
            transform_augmentation_rot_resolution
        )
        self._optimizer_type = optimizer_type
        # for SLAP baseline
        self._num_points = num_pts

        self._cross_entropy_loss = nn.CrossEntropyLoss(reduction="none")
        # for visualizations
        self.cam_view = {
            "front": [-0.89795424592554529, 0.047678244807235863, 0.43749852250766141],
            "lookat": [0.33531651482385966, 0.048464899929339826, 0.54704503365806367],
            "up": [0.43890929711345494, 0.024286597087151203, 0.89820308956788786],
            "zoom": 0.43999999999999972,
        }

    def build(self, training: bool, device: torch.device = None):
        self._training = training
        self._device = device

        # Create a voxel grid for processing things here
        # reference --
        # vox_grid = VoxelGrid(
        #     coord_bounds=self.scene_bounds,
        #     voxel_size=self.voxel_sizes[0],
        #     device=self.device,
        #     batch_size=1,
        #     feature_size=3,
        #     max_num_coords=20000,  # self.num_pts
        # )
        vox_grid = VoxelGrid(
            coord_bounds=self._coordinate_bounds,
            voxel_size=self._voxel_size,
            device=device,
            batch_size=self._batch_size,
            feature_size=self._voxel_feature_size,
            max_num_coords=self._num_points,
        )
        self._vox_grid = vox_grid

        self._q = (
            QFunction(
                self._perceiver_encoder,
                vox_grid,
                self._rotation_resolution,
                device,
                training,
            )
            .to(device)
            .train(training)
        )

        self._coordinate_bounds = torch.tensor(
            self._coordinate_bounds, device=device
        ).unsqueeze(0)

        if self._optimizer_type == "lamb":
            # From: https://github.com/cybertronai/pytorch-lamb/blob/master/pytorch_lamb/lamb.py
            self._optimizer = Lamb(
                self._q.parameters(),
                lr=self._lr,
                weight_decay=self._lambda_weight_l2,
                betas=(0.9, 0.999),
                adam=False,
            )
        elif self._optimizer_type == "adam":
            self._optimizer = torch.optim.Adam(
                self._q.parameters(),
                lr=self._lr,
                weight_decay=self._lambda_weight_l2,
            )
        else:
            raise Exception("Unknown optimizer")
        with torch.no_grad():
            self.clip_model, self.preprocess = clip.load(
                # "RN50", device=self.device  # network used in peract
                "ViT-B/32",
                device=self._device,  # network used in SLAP
            )

    def _softmax_q(self, q):
        q_shape = q.shape
        return F.softmax(q.reshape(q_shape[0], -1), dim=1).reshape(q_shape)

    def save_weights(self, filename):
        torch.save(self._q.state_dict(), filename)

    def load_weights(self, filename):
        self._q.load_state_dict(torch.load(filename))

    def _get_one_hot_expert_actions(
        self,  # You don't really need this function since GT labels are already in the right format. This is some leftover code from my experiments with label smoothing.
        batch_size,
        action_trans,
        action_rot_grip,
        action_ignore_collisions,
        device,
    ):
        bs = batch_size

        # initialize with zero tensors
        action_trans_one_hot = torch.zeros(
            (bs, self._voxel_size, self._voxel_size, self._voxel_size),
            dtype=int,
            device=device,
        )
        action_rot_x_one_hot = torch.zeros(
            (bs, self._num_rotation_classes), dtype=int, device=device
        )
        action_rot_y_one_hot = torch.zeros(
            (bs, self._num_rotation_classes), dtype=int, device=device
        )
        action_rot_z_one_hot = torch.zeros(
            (bs, self._num_rotation_classes), dtype=int, device=device
        )
        action_grip_one_hot = torch.zeros((bs, 2), dtype=int, device=device)
        action_collision_one_hot = torch.zeros((bs, 2), dtype=int, device=device)

        # fill one-hots
        for b in range(bs):
            # translation
            gt_coord = action_trans[b, :]
            action_trans_one_hot[b, gt_coord[0], gt_coord[1], gt_coord[2]] = 1

            # rotation
            gt_rot_grip = action_rot_grip[b, :]
            action_rot_x_one_hot[b, gt_rot_grip[0]] = 1
            action_rot_y_one_hot[b, gt_rot_grip[1]] = 1
            action_rot_z_one_hot[b, gt_rot_grip[2]] = 1
            action_grip_one_hot[b, gt_rot_grip[3]] = 1

            # ignore collision
            gt_ignore_collisions = action_ignore_collisions[b, :]
            action_collision_one_hot[b, gt_ignore_collisions[0]] = 1

        # flatten trans
        action_trans_one_hot = action_trans_one_hot.view(bs, -1)

        return (
            action_trans_one_hot,
            action_rot_x_one_hot,
            action_rot_y_one_hot,
            action_rot_z_one_hot,
            action_grip_one_hot,
            action_collision_one_hot,
        )

    def _norm_rgb(x):
        return (x.float() / 255.0) * 2.0 - 1.0

    def _preprocess_inputs(replay_sample):
        obs, pcds = [], []
        for n in CAMERAS:
            rgb = utils.stack_on_channel(replay_sample["%s_rgb" % n])
            pcd = utils.stack_on_channel(replay_sample["%s_point_cloud" % n])

            rgb = _norm_rgb(rgb)

            obs.append(
                [rgb, pcd]
            )  # obs contains both rgb and pointcloud (used in ARM for other baselines)
            pcds.append(pcd)  # only pointcloud
        return obs, pcds

    # extract CLIP language features for goal string
    def _clip_encode_text(self, text):
        x = self.clip_model.token_embedding(text).type(
            self.clip_model.dtype
        )  # [batch_size, n_ctx, d_model]

        x = x + self.clip_model.positional_embedding.type(self.clip_model.dtype)
        x = x.permute(1, 0, 2)  # NLD -> LND
        x = self.clip_model.transformer(x)
        x = x.permute(1, 0, 2)  # LND -> NLD
        x = self.clip_model.ln_final(x).type(self.clip_model.dtype)

        emb = x.clone()
        x = (
            x[torch.arange(x.shape[0]), text.argmax(dim=-1)]
            @ self.clip_model.text_projection
        )

        return x, emb

    def clip_encode_text(self, text):
        """encode text as a sequence"""

        with torch.no_grad():
            lang = clip.tokenize(text).to(self._device)
            lang = self.clip_model.token_embedding(lang).type(
                self.clip_model.dtype
            ) + self.clip_model.positional_embedding.type(self.clip_model.dtype)
            lang = lang.permute(1, 0, 2)
            lang = self.clip_model.transformer(lang)
            lang = lang.permute(1, 0, 2)
            lang = self.clip_model.ln_final(lang).type(self.clip_model.dtype)

        # We now have per-word clip embeddings
        lang = lang.float()

        # Encode language here
        batch_size, lang_seq_len, _ = lang.shape
        # lang = lang.view(batch_size * lang_seq_len, -1)
        # learned pos encodings will be added in PerAct
        # if self.learned_pos_encoding:
        #     lang = self.lang_preprocess(lang) + self.pos_encoding
        # else:
        #     lang = self.lang_preprocess(lang)
        #     lang = self.pos_encoding(lang)
        lang = lang.view(batch_size, lang_seq_len, -1)

        return lang

    def show_prediction(
        self,
        xyz,
        rgb,
        pred_keypt_orig,
        pred_keypt_rot,
        save=False,
        viewpt={},
    ):
        if np.any(rgb) > 1:
            rgb = rgb / 255.0
        pcd = numpy_to_pcd(xyz, rgb)
        geoms = [pcd]
        coords = o3d.geometry.TriangleMesh.create_coordinate_frame(
            size=0.1, origin=pred_keypt_orig
        )
        coords = coords.rotate(pred_keypt_rot)
        geoms.append(coords)
        o3d.visualization.draw(geoms)
        # vis = o3d.visualization.Visualizer()
        # vis.create_window()
        # for geom in geoms:
        #     vis.add_geometry(geom)
        #     vis.update_geometry(geom)
        # if viewpt:
        #     ctr = vis.get_view_control()
        #     ctr.set_front(viewpt["front"])
        #     ctr.set_lookat(viewpt["lookat"])
        #     ctr.set_up(viewpt["up"])
        #     ctr.set_zoom(viewpt["zoom"])
        # if save:
        #     vis.poll_updates()
        #     vis.update_renderer()
        #     vis.capture_screen_image(f"/home/robopen08/.larp/{self.name}.png")
        # else:
        #     vis.run()
        # vis.destroy_window()
        # del vis
        # if ctr:
        #     del ctr

    def update_for_rollout(self, batch: dict, center) -> dict:
        lang = batch["cmd"]
        rgb = np.copy(batch["rgb"].numpy())
        xyz = np.copy(batch["xyz"].numpy())
        batch = {
            k: v.to(self._device) for k, v in batch.items() if type(v) == torch.Tensor
        }
        batch["cmd"] = lang
        n, rgb_dim = batch["centered_rgb"].shape
        batch["centered_rgb"] = batch["centered_rgb"].view(1, n, rgb_dim)
        batch["centered_rgb"] = (batch["centered_rgb"].float() / 1.0) * 2.0 - 1.0
        _, xyz_dim = batch["centered_xyz"].shape
        batch["centered_xyz"] = batch["centered_xyz"].view(1, n, xyz_dim)
        batch["xyz"] = batch["centered_xyz"]
        batch["rgb"] = batch["centered_rgb"]
        update_dict = self.update(0, batch, backprop=False)
        # discrete to continuous
        continuous_trans = (
            update_dict["pred_action"]["continuous_trans"][0].detach().cpu().numpy()
        )
        continuous_quat = discrete_euler_to_quaternion(
            update_dict["pred_action"]["rot_and_grip"][0][:3].detach().cpu().numpy(),
            resolution=self._rotation_resolution,
        )
        gripper_open = bool(
            update_dict["pred_action"]["rot_and_grip"][0][-1].detach().cpu().numpy()
        )
        # breakpoint()
        x, y, z, w = continuous_quat
        pred_ori = tra.quaternion_matrix([w, x, y, z])[:3, :3]
        pred_pos = center + continuous_trans
        # TODO can add a visualization here to only roll out reasonable outputs
        # rgb = batch["rgb"][0].detach().cpu().numpy()
        # xyz = batch["xyz"][0].detach().cpu().numpy()
        print(f"{lang}")
        print(f"Predicted gripper state: {gripper_open}")
        self.show_prediction(
            xyz,
            rgb,
            pred_pos,
            pred_ori,
            viewpt=self.cam_view,
            save=False,
        )

        action_dict = {
            "predicted_pos": pred_pos,
            "predicted_ori": pred_ori[:3, :3],
            "gripper_act": gripper_open,
        }
        print(action_dict)
        return action_dict, update_dict

    def update(
        self, step: int, replay_sample: dict, backprop: bool = True, val=False
    ) -> dict:
        """
        This is what we run to train the model
        Also for inference
        Pay attention here

        Params:
        -------
        step: means something if you are training, not used at inference time
        replay_sample: a single observation
        backprop: true if training, false if inference
        """
        # sample
        action_trans = None
        if val or backprop:
            # This gives us our action translation indices - location in the voxel cube
            action_trans = replay_sample["trans_action_indices"][:, :, :3].int()
            # Rotation index
            action_rot_grip = replay_sample["rot_grip_action_indices"][0, :].int()
            # Do we take some action to ignore collisions or not
            action_ignore_collisions = replay_sample["ignore_collisions"][:, :, -1].int()

        # Get language goal embedding
        lang_goal = replay_sample["cmd"]
        lang_goal_embs = self.clip_encode_text(lang_goal)

        obs = replay_sample["rgb"]
        pcd = replay_sample["xyz"]

        # metric scene bounds
        bounds = self._coordinate_bounds

        # inputs
        # 4 dimensions - proprioception: {gripper_open, left_finger_joint, right_finger_joint, timestep}
        # TODO: edit such that update happens 1x per action
        proprio = replay_sample["gripper_states"]

        # NOTE: PerAct data augmentation is replaced by ours alongwith a check to
        # make sure that action is valid

        # Q function
        # This is where we will be computing our q function
        # from observation - collision etc all go here
        # q_trans is going to be the 100 x 100 x 100 q function
        # Rot_grip_q is size 218 - X Y Z, 5 degree bins for each
        #  72 bins per x,y,z
        # collision_q is a "binary" (2 values)
        # This is purely supervised and comes from oracle data
        total_loss = 0.0
        num_iters = 0
        continuous_trans_vector = []
        translation_vector = []
        rot_and_grip_vector = []
        for idx in range(len(replay_sample["ee_keyframe_pos"])):
            proprio_instance = proprio[:, idx]
            action_trans_instance = action_trans[:, idx]
            action_rot_grip_instance = action_rot_grip[idx].unsqueeze(0)
            action_ignore_collissions_instance = action_ignore_collisions[idx].unsqueeze(0)
            q_trans, rot_grip_q, collision_q, voxel_grid = self._q(
                obs, proprio_instance, pcd, lang_goal_embs, bounds
            )

            # one-hot expert actions
            bs = self._batch_size
            if val or backprop:
                # Convert expert x, y, z into 1 hot vectors
                (
                    action_trans_one_hot,
                    action_rot_x_one_hot,
                    action_rot_y_one_hot,
                    action_rot_z_one_hot,
                    action_grip_one_hot,
                    action_collision_one_hot,
                ) = self._get_one_hot_expert_actions(
                    bs,
                    action_trans_instance,
                    action_rot_grip_instance,
                    action_ignore_collissions_instance,
                    device=self._device,
                )
            if val or backprop:
                # cross-entropy loss
                trans_loss = self._cross_entropy_loss(
                    q_trans.view(bs, -1), action_trans_one_hot.argmax(-1)
                )

                rot_grip_loss = 0.0
                rot_grip_loss += self._cross_entropy_loss(
                    rot_grip_q[
                        :,
                        0 * self._num_rotation_classes : 1 * self._num_rotation_classes,
                    ],
                    action_rot_x_one_hot.argmax(-1),
                )
                rot_grip_loss += self._cross_entropy_loss(
                    rot_grip_q[
                        :,
                        1 * self._num_rotation_classes : 2 * self._num_rotation_classes,
                    ],
                    action_rot_y_one_hot.argmax(-1),
                )
                rot_grip_loss += self._cross_entropy_loss(
                    rot_grip_q[
                        :,
                        2 * self._num_rotation_classes : 3 * self._num_rotation_classes,
                    ],
                    action_rot_z_one_hot.argmax(-1),
                )
                rot_grip_loss += self._cross_entropy_loss(
                    rot_grip_q[:, 3 * self._num_rotation_classes :],
                    action_grip_one_hot.argmax(-1),
                )

                collision_loss = self._cross_entropy_loss(
                    collision_q, action_collision_one_hot.argmax(-1)
                )

                total_loss += trans_loss + rot_grip_loss + 0*collision_loss
                num_iters += 1
                # choose best action through argmax
                (
                    coords_indicies,
                    rot_and_grip_indicies,
                    ignore_collision_indicies,
                ) = self._q.choose_highest_action(q_trans, rot_grip_q, collision_q)

                # discrete to continuous translation action
                res = (bounds[:, 3:] - bounds[:, :3]) / self._voxel_size
                continuous_trans = bounds[:, :3] + res * coords_indicies.int() + res / 2

                continuous_trans_vector.append(continuous_trans)
                translation_vector.append(coords_indicies)
                rot_and_grip_vector.append(rot_and_grip_indicies)

        total_loss = (total_loss / num_iters).mean()

        # backprop
        if backprop:
            self._optimizer.zero_grad()
            total_loss.backward()
            self._optimizer.step()

        total_loss = total_loss.item()

        return {
            "total_loss": total_loss,
            "voxel_grid": voxel_grid,
            "q_trans": self._softmax_q(q_trans),
            "pred_action": {
                "trans": translation_vector,
                "continuous_trans": continuous_trans_vector,
                "rot_and_grip": rot_and_grip_vector,
                # "collision": ignore_collision_indicies,
            },
            "expert_action": {"action_trans": action_trans},
        }


def train(path, split_path, wt_path):
    # hydra_cfg = hydra.core.hydra_config.HydraConfig.get()
    # hydra_output_dir = hydra_cfg["runtime"]["output_dir"]
    hydra_output_dir = os.getcwd()
    device = "cuda" if torch.cuda.is_available() else "cpu"

    # enter our own dataloader
    # data_dir = (
    #     "/home/priparashar/robopen_h5s/larp/kinesthetic_dataset/all_tasks/kin_data"
    # )
    # data_dir = "/home/cpaxton/data/kinesthetic_dataset"
    # split_file = "./assets/task_splits/main_split.yaml"
    with open(split_path, "r") as f:
        train_test_split = yaml.safe_load(f)
    ds = PerActRobotDataset(
        path,
        template="**/*.h5",
        # verbose=True,
        num_pts=8000,
        data_augmentation=True,
        crop_radius=False,
        ori_dr_range=np.pi / 8,
        cart_dr_range=0.0,
        first_frame_as_input=False,
        # first_keypoint_only=True,
        # keypoint_range=[0],
        trial_list=train_test_split["train"] if split_path else [],
        orientation_type="quaternion",
        show_voxelized_input_and_reference=False,
        show_cropped=False,
        verbose=False,
        multi_step=True,
        visualize_interaction_estimates=False,
        visualize_cropped_keyframes=False,
        robot="stretch",
        autoregressive=True,
        time_as_one_hot=True,
    )
    # Create data loaders
    num_workers = 10
    B = 1
    data_loader = torch.utils.data.DataLoader(
        ds,
        batch_size=B,
        num_workers=num_workers,
        shuffle=True,
        drop_last=True,
    )

    # initialize PerceiverIO Transformer
    perceiver_encoder = PerceiverIO(
        depth=6,
        iterations=1,
        voxel_size=VOXEL_SIZES[0],
        initial_dim=3 + 3 + 1 + 3,
        low_dim_size=3,
        layer=0,
        num_rotation_classes=72,
        num_grip_classes=2,
        num_collision_classes=2,
        num_latents=NUM_LATENTS,
        latent_dim=512,
        cross_heads=1,
        latent_heads=8,
        cross_dim_head=64,
        latent_dim_head=64,
        weight_tie_layers=False,
        activation="lrelu",
        input_dropout=0.1,
        attn_dropout=0.1,
        decoder_dropout=0.0,
        voxel_patch_size=5,
        voxel_patch_stride=5,
        final_dim=64,
    )
    # initialize PerceiverActor
    peract_agent = PerceiverActorAgent(
        coordinate_bounds=ds.scene_bounds,
        perceiver_encoder=perceiver_encoder,
        camera_names=CAMERAS,
        batch_size=BATCH_SIZE,
        voxel_size=ds.voxel_sizes[0],
        voxel_feature_size=3,
        num_rotation_classes=72,
        rotation_resolution=5,
        lr=0.0001,
        image_resolution=[IMAGE_SIZE, IMAGE_SIZE],
        lambda_weight_l2=0.000001,
        transform_augmentation=False,
        optimizer_type="lamb",
        num_pts=8000,
    )
    peract_agent.build(training=True, device=device)

    # basic test before training
    # for batch in data_loader:
    #     ds.visualize_data(batch, peract_agent._vox_grid)
    #     res = input("Press enter if looks ok; n to exit")
    #     if res == "n":
    #         return
    #     break

    LOG_FREQ = 1
    TRAINING_ITERATIONS = 100

    if wt_path:
        peract_agent.load_weights(wt_path)
        print(f"---> loaded last best {wt_path} <---")

    start_time = time.time()
    iter = 0
    filename_prefix = "multi-task"
    while iter < TRAINING_ITERATIONS:
        for batch in tqdm(data_loader, ncols=50):
            if not batch["data_ok_status"] or not ds.is_action_valid(batch):
                print(f"Skipping {iter} as action is not valid")
                continue
            desc = batch["cmd"]
            batch = {
                k: v.to(device) for k, v in batch.items() if type(v) == torch.Tensor
            }
            batch["cmd"] = desc
            update_dict = peract_agent.update(iter, batch)

        if iter % LOG_FREQ == 0:
            elapsed_time = (time.time() - start_time) / 60.0
            if iter > 0:
                per_iter_time = elapsed_time / float(iter)
            else:
                per_iter_time = elapsed_time
            print(
                "Iteration %d | Total Loss: %f | Elapsed Time: %f mins | Elapsed time per iter: %f mins"
                % (iter, update_dict["total_loss"], elapsed_time, per_iter_time)
            )
            filename = os.path.join(
                hydra_output_dir, filename_prefix + f"_{iter}_{int(elapsed_time)}.pth"
            )
            peract_agent.save_weights(filename)
            print(f"Written to {filename}")
            filename = os.path.join(hydra_output_dir, filename_prefix + "_best.pth")
            peract_agent.save_weights(filename)
            print(f"Written best to {filename}")
        iter += 1
    filename = os.path.join(hydra_output_dir, filename_prefix + ".pth")
    peract_agent.save_weights(filename)
    print(f"Written last to {filename}")


def eval(path, visualize):
    device = "cuda" if torch.cuda.is_available() else "cpu"

    # enter our own dataloader
    data_dir = (
        "/home/priparashar/robopen_h5s/larp/kinesthetic_dataset/all_tasks/kin_data"
    )
    # data_dir = "/home/cpaxton/data/kinesthetic_dataset"
    split_file = "./assets/task_splits/main_split.yaml"
    with open(split_file, "r") as f:
        train_test_split = yaml.safe_load(f)
    ds = PerActRobotDataset(
        data_dir,
        template="**/*.h5",
        data_augmentation=False,
        trial_list=train_test_split["val"],
        verbose=True,
        num_pts=8000,
    )
    # Create data loaders
    num_workers = 12
    B = 1
    data_loader = torch.utils.data.DataLoader(
        ds,
        batch_size=B,
        num_workers=num_workers,
        shuffle=True,
        drop_last=True,
    )
    # initialize PerceiverIO Transformer
    perceiver_encoder = PerceiverIO(
        depth=6,
        iterations=1,
        voxel_size=VOXEL_SIZES[0],
        initial_dim=3 + 3 + 1 + 3,
        low_dim_size=3,
        layer=0,
        num_rotation_classes=72,
        num_grip_classes=2,
        num_collision_classes=2,
        num_latents=NUM_LATENTS,
        latent_dim=512,
        cross_heads=1,
        latent_heads=8,
        cross_dim_head=64,
        latent_dim_head=64,
        weight_tie_layers=False,
        activation="lrelu",
        input_dropout=0.1,
        attn_dropout=0.1,
        decoder_dropout=0.0,
        voxel_patch_size=5,
        voxel_patch_stride=5,
        final_dim=64,
    )
    # intitialize peract_agent
    peract_agent = PerceiverActorAgent(
        coordinate_bounds=ds.scene_bounds,
        perceiver_encoder=perceiver_encoder,
        camera_names=CAMERAS,
        batch_size=BATCH_SIZE,
        voxel_size=ds.voxel_sizes[0],
        voxel_feature_size=3,
        num_rotation_classes=72,
        rotation_resolution=5,
        lr=0.0001,
        image_resolution=[IMAGE_SIZE, IMAGE_SIZE],
        lambda_weight_l2=0.000001,
        transform_augmentation=False,
        optimizer_type="lamb",
        num_pts=8000,
    )
    peract_agent.build(training=False, device=device)
    # load _q weights from saved file
    if path:
        files = glob.glob(os.path.join(path, "*.pth"))
    best_loss = None
    best_model = None
    with torch.no_grad():
        for file in tqdm(files, ncols=50):
            peract_agent.load_weights(file)
            loss = 0.0
            tot = 0
            for batch in data_loader:
                lang_goal = batch["cmd"]
                if (
                    "place" in lang_goal[0]
                    or "put" in lang_goal[0]
                    or "add" in lang_goal[0]
                ):
                    continue
                batch = {
                    k: v.to(device) for k, v in batch.items() if type(v) == torch.Tensor
                }
                batch["cmd"] = lang_goal
                update_dict = peract_agent.update(0, batch, backprop=False, val=True)
                # extract prediction
                expert_pos = batch["ee_keyframe_pos"][0].detach().cpu().numpy()
                if "total_loss" in update_dict.keys():
                    loss += update_dict["total_loss"]
                    tot += 1
                if visualize:
                    print(f"Lang Goal: {lang_goal}")
                    # things to visualize
                    vis_voxel_grid = update_dict["voxel_grid"][0].detach().cpu().numpy()
                    vis_trans_q = update_dict["q_trans"][0].detach().cpu().numpy()
                    vis_trans_coord = (
                        update_dict["pred_action"]["trans"][0].detach().cpu().numpy()
                    )
                    vis_gt_coord = (
                        update_dict["expert_action"]["action_trans"][0]
                        .detach()
                        .cpu()
                        .numpy()
                    )
                    # discrete to continuous
                    continuous_trans = (
                        update_dict["pred_action"]["continuous_trans"][0]
                        .detach()
                        .cpu()
                        .numpy()
                    )
                    continuous_quat = discrete_euler_to_quaternion(
                        update_dict["pred_action"]["rot_and_grip"][0][:3]
                        .detach()
                        .cpu()
                        .numpy(),
                        resolution=peract_agent._rotation_resolution,
                    )
                    gripper_open = bool(
                        update_dict["pred_action"]["rot_and_grip"][0][-1]
                        .detach()
                        .cpu()
                        .numpy()
                    )
                    # ignore_collision = bool(
                    #     update_dict["pred_action"]["collision"][0][0]
                    #     .detach()
                    #     .cpu()
                    #     .numpy()
                    # )

                    # gripper visualization pose
                    voxel_size = 0.045
                    voxel_scale = voxel_size * 100
                    # gripper_pose_mat = get_gripper_render_pose(
                    #     voxel_scale,
                    #     ds.scene_bounds[:3],
                    #     continuous_trans,
                    #     continuous_quat,
                    # )
                    gripper_open = None

                    # @markdown #### Show Q-Prediction and Best Action
                    show_expert_action = True  # @param {type:"boolean"}
                    show_q_values = True  # @param {type:"boolean"}
                    render_gripper = True  # @param {type:"boolean"}
                    rotation_amount = (
                        0  # @param {type:"slider", min:-180, max:180, step:5}
                    )

                    rendered_img = visualise_voxel(
                        vis_voxel_grid,
                        vis_trans_q if show_q_values else None,
                        vis_trans_coord,
                        vis_gt_coord if show_expert_action else None,
                        voxel_size=voxel_size,
                        rotation_amount=np.deg2rad(rotation_amount),
                        # render_gripper=render_gripper,
                        gripper_pose=gripper_pose_mat,
                        gripper_mesh_scale=voxel_scale,
                    )

                    fig = plt.figure(figsize=(15, 15))
                    plt.imshow(rendered_img)
                    plt.pause(8.00)
                    plt.axis("off")
            if best_loss is None:
                best_loss = loss / tot
                best_model = file
            elif best_loss > (loss / tot):
                best_loss = loss / tot
                best_model = file
            print(file, " ", loss / tot)
        print(best_model)


@click.command()
@click.option("-f", "--flag")
@click.option("-p", "--path", type=str, default=None)
@click.option("-sp", "--split-path", type=str, default=None)
@click.option("-wp", "--weight-path", type=str, default=None)
@click.option("--viz/--no-viz", default=False)
def main(flag, path, viz, split_path, weight_path):
    if flag == "t":
        train(path, split_path, weight_path)
    elif flag == "e":
        eval(path, viz)


if __name__ == "__main__":
    main()
