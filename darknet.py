import re

import numpy as np
import torch
import torch.nn.functional as F


class DummyLayer(torch.nn.Module):
    def __init__(self):
        """
        Empty dummy layer to serve as stand-in for YOLO shortcut and route
        layers, which provide connections to previous layers. Actual logic
        is handled in Darknet.forward().
        """
        super().__init__()


class MaxPool2dPad(torch.nn.MaxPool2d):
    """
    Hacked MaxPool2d class to replicate "same" padding; refer to
    https://github.com/eriklindernoren/PyTorch-YOLOv3/pull/48/files#diff-f219bfe69e6ed201e4bdfdb371dc0c9bR49
    """
    def forward(self, input_):
        if self.kernel_size > 1 and self.stride == 1:
            padding = self.kernel_size - 1
            zero_pad = torch.nn.ZeroPad2d((0, padding, 0, padding))
            input_ = zero_pad(input_)
        return F.max_pool2d(
            input_, self.kernel_size, self.stride, self.padding,
            self.dilation, self.ceil_mode, self.return_indices
        )


class YOLOLayer(torch.nn.Module):
    def __init__(self, anchors, mask, device="cuda"):
        """
        anchors (list): List of bounding box anchors (2-tuples of floats).
        mask (list): Anchor box indices (corresponding to `anchors`) for
            which to make predictions.
        device (str): device (eg, "cpu", "cuda") on which to allocate tensors.
        """
        super().__init__()
        self.anchors = [anchors[anchor_idx] for anchor_idx in mask]
        self.mask = mask
        self.device = device

    def forward(self, x):
        """
        Process input tensor and produce bounding box coordinates,
        predicted class score (probability), and class index.

        Returns:
            bbox_xywhs: Mx5 tensor of M detections, where dim 1 indices
                correspond to: center x, center y, width, height, class
                probability (of class with greatest probability).
                0 <= x, y <= 1 and correspond to fraction of image size.
                w, h are absolute pixel sizes (and should be scaled to net
                info training image width/height).
            max_class_idx: M element tensor corresponding index of class with
                max probability for each of M detections.
        """
        # Introspect number of classes from anchors and input shape.
        num_anchors = len(self.anchors)
        num_samples, num_predictions, h, w = x.shape
        num_classes = int(num_predictions / num_anchors) - 5
        x = x.reshape((num_samples, num_anchors, num_classes + 5, h, w))

        # Indices 0-3 corresponds to xywh energies, index 4 corresponds to
        # objectness energy, and 5: correspond to class energies.
        xywh_energy = x[:, :, 0:4, :, :]
        obj_energy = x[:, :, 4:5, :, :]
        class_energy = x[:, :, 5:, :, :]

        bbox_xywh = xywh_energy.clone().detach()

        # Cell offsets C_x and C_y.
        cx = torch.linspace(0, w - 1, w, device=self.device).repeat(h, 1)
        cy = torch.linspace(
            0, h - 1, h, device=self.device
        ).repeat(w, 1).t().contiguous()

        # Get bbox center x and y coordinates.
        bbox_xywh[:, :, 0, :, :].sigmoid_().add_(cx).div_(w)
        bbox_xywh[:, :, 1, :, :].sigmoid_().add_(cy).div_(h)

        # Anchor priors P_w and P_h.
        anchors = self.anchors

        anchor_w = torch.tensor(
            anchors, device=self.device
        )[:, 0].reshape(1, num_anchors, 1, 1)

        anchor_h = torch.tensor(
            anchors, device=self.device
        )[:, 1].reshape(1, num_anchors, 1, 1)

        # Get bbox width and height.
        bbox_xywh[:, :, 2, :, :].exp_().mul_(anchor_w)
        bbox_xywh[:, :, 3, :, :].exp_().mul_(anchor_h)

        # Get objectness and class scores.
        obj_score = obj_energy.clone().detach().sigmoid()
        class_score = F.softmax(class_energy.clone().detach(), dim=2)

        max_class_score, max_class_idx = torch.max(class_score, 2, keepdim=True)
        max_class_score.mul_(obj_score)

        # Flatten the resulting tensors along anchor box prior and grid cell
        # dimensions. This makes it easier to combine predictions from this
        # scale (i.e., this YOLO layer) with predictions from other scales
        # in Darknet.forward().
        bbox_xywh = bbox_xywh.squeeze().permute(1, 0, 2, 3).reshape(4, -1).T
        max_class_score = max_class_score.flatten().unsqueeze(1)
        max_class_idx = max_class_idx.flatten()

        # Concatenate bbox coordinates and max class scores such that indices
        # 0-3 of each bbox correspond to its xywh coordinates and index 4 of
        # each bbox corresponds to its max class score (i.e., confidence).
        bbox_xywhs = torch.cat((bbox_xywh, max_class_score), dim=1)

        return bbox_xywhs, max_class_idx


def parse_config(fpath):
    """
    Parse Darknet config file and return a list of network blocks and
    pertinent information for each block.

    Args:
        fpath (str): Path to Darknet .cfg file.

    Returns:
        List of blocks and network info.
    """

    with open(fpath, "r") as f:
        # Ignore lines consisting only of whitespace or commented lines.
        lines = [
            line.strip() for line in f.readlines()
            if not (line.isspace() or line.startswith("#"))
        ]

    # Each block begins with a line of the form "[type]", with the block type
    # (eg, "convolutional") enclosed in square brackets. Chunk config text
    # into blocks.
    block_start_lines = [
        line_num for line_num, line in enumerate(lines) if line.startswith("[")
    ]
    block_start_lines.append(len(lines))

    text_blocks = []
    for i in range(1, len(block_start_lines)):
        block_start, block_end = block_start_lines[i-1], block_start_lines[i]
        text_blocks.append(lines[block_start:block_end])

    def str2type(raw_val):
        """
        Helper function to convert a string input to the appropriate
        type (str, int, or float).
        """
        try:
            return int(raw_val)
        except ValueError:
            pass

        try:
            return float(raw_val)
        except ValueError:
            return raw_val

    blocks = []
    net_info = None
    for text_block in text_blocks:
        block = {"type": text_block[0][1:-1]}
        for line in text_block[1:]:
            key, raw_val = line.split("=")
            key = key.strip()

            # Convert fields with multiple comma-separated values into lists.
            if "," in raw_val:
                val = [str2type(item.strip()) for item in raw_val.split(",")]
            else:
                val = str2type(raw_val.strip())

            # If this is a "route" block, its "layers" field
            # contains either a single integer or several integers. If single
            # integer, make it a list for convenience (avoids having to check
            # type when creating modules and running net.forward(), etc.).
            if (
                block["type"] == "route"
                and key == "layers"
                and isinstance(val, int)
            ):
                val = [val]

            # If this is a "yolo" block, it contains an "anchors" field
            # consisting of pairs of anchors; group anchors into chunks of two.
            if key == "anchors":
                val = [val[i:i+2] for i in range(0, len(val), 2)]

            block[key] = val

        if block["type"] == "net":
            net_info = block
        else:
            blocks.append(block)

    return blocks, net_info


def blocks2modules(blocks, net_info):
    """
    Translate output of `parse_config()` into pytorch modules.

    Returns:
        torch.nn.modules.container.ModuleList
    """
    modules = torch.nn.ModuleList()
    
    # Track number of channels (filters) in the output of each layer; this
    # information is necessary to determine layer input/output shapes for
    # various layers.
    curr_out_channels = None
    prev_layer_out_channels = net_info["channels"]
    out_channels_list = []

    for i, block in enumerate(blocks):
        module = torch.nn.Sequential()

        if block["type"] == "convolutional":
            batch_normalize = "batch_normalize" in block
            bias = not batch_normalize
            kernel_size = block["size"]
            padding = (kernel_size - 1) // 2 if "pad" in block else 0
            in_channels = prev_layer_out_channels
            out_channels = block["filters"]

            conv = torch.nn.Conv2d(
                in_channels=in_channels, out_channels=out_channels,
                kernel_size=kernel_size, stride=block["stride"],
                padding=padding, bias=bias
            )
            module.add_module("conv_{}".format(i), conv)

            if batch_normalize:
                bn = torch.nn.BatchNorm2d(num_features=out_channels)
                module.add_module("batch_norm_{}".format(i), bn)

            if block["activation"] == "leaky":
                acti = torch.nn.LeakyReLU(negative_slope=0.1, inplace=True)
                module.add_module("leaky_{}".format(i), acti)
            elif block["activation"] == "linear":
                # NOTE: Darknet src files call out "linear" vs "relu" but we
                # use ReLU here.
                acti = torch.nn.ReLU(inplace=True)

            # Update the number of current (output) channels.
            curr_out_channels = out_channels
        
        elif block["type"] == "maxpool":
            stride = block["stride"]
            maxpool = MaxPool2dPad(
                kernel_size=block["size"], stride=stride
            )
            module.add_module("maxpool_{}".format(i), maxpool)

        elif block["type"] == "route":
            # Route layer concatenates outputs (across channel dim); add dummy
            # layer and handle the actual logic in Darknet.forward().
            module.add_module("route_{}".format(i), DummyLayer())

            out_channels = sum(
                out_channels_list[layer_idx] for layer_idx in block["layers"]
            )

            curr_out_channels = out_channels

        elif block["type"] == "shortcut":
            # Shortcut layer is a residual layer that sums output from previous
            # layer and a different previous layers; add dummy layer and handle
            # the actual logic in Darknet.forward().
            module.add_module("shortcut_{}".format(i), DummyLayer())

            if "activation" in block:
                if block["activation"] == "leaky":
                    acti = torch.nn.LeakyReLU(negative_slope=0.1, inplace=True)
                    module.add_module("leaky_{}".format(i), acti)
                elif block["activation"] == "linear":
                    acti = torch.nn.ReLU(inplace=True)

            assert out_channels == out_channels_list[i + block["from"]]
            curr_out_channels = out_channels

        elif block["type"] == "upsample":
            # NOTE: torch.nn.Upsample is deprecated in favor of Interpolate;
            # consider using this and/or other interpolation methods?
            upsample = torch.nn.Upsample(
                scale_factor=block["stride"], mode="nearest"
            )
            module.add_module("upsample_{}".format(i), upsample)

        elif block["type"] == "yolo":
            yolo = YOLOLayer(block["anchors"], block["mask"])
            module.add_module("yolo_{}".format(i), yolo)

        modules.append(module)
        prev_layer_out_channels = curr_out_channels
        out_channels_list.append(curr_out_channels)

    return modules


class Darknet(torch.nn.Module):
    def __init__(self, config_fpath, device="cuda"):
        """
        Args:
            config_path (str): Path to Darknet .cfg file.
            device (str): Device (eg, "cpu", "cuda") on which to allocate
                tensors.
        """
        super().__init__()
        self.blocks, self.net_info = parse_config(config_fpath)
        self.modules_ = blocks2modules(self.blocks, self.net_info)
        self.device = device

        # Determine the indices of the layers that will have to be cached
        # for route and shortcut connections.
        self.blocks_to_cache = set()
        for i, block in enumerate(self.blocks):
            if block["type"] == "route":
                # Replace negative values to reflect absolute (positive) block idx.
                for j, block_idx in enumerate(block["layers"]):
                    if block_idx < 0:
                        block["layers"][j] = i + block_idx
                        self.blocks_to_cache.add(i + block_idx)
                    else:
                        self.blocks_to_cache.add(block_idx)
            elif block["type"] == "shortcut":
                # "shortcut" layer concatenates the feature map from the
                # previous block with the feature map specified by the shortcut
                # block's "from" field (which is a negative integer/offset).
                self.blocks_to_cache.add(i - 1)
                self.blocks_to_cache.add(i + block["from"])

    def forward(self, x):
        """
        Returns:
            Dictionary of bbox coordinates/scores and bbox class indices.
        """
        cached_outputs = {block_idx: None for block_idx in self.blocks_to_cache}

        bbox_list, max_class_idx_list = [], []
        for i, block in enumerate(self.blocks):
            if block["type"] in ("convolutional", "maxpool", "upsample"):
                x = self.modules_[i](x)
            elif block["type"] == "route":
                x = torch.cat(
                    tuple(cached_outputs[idx] for idx in block["layers"]),
                    dim=1
                )
            elif block["type"] == "shortcut":
                x = cached_outputs[i-1] + cached_outputs[i+block["from"]]
            elif block["type"] == "yolo":
                bbox_xywhs, max_class_idx = self.modules_[i](x)
                bbox_list.append(bbox_xywhs)
                max_class_idx_list.append(max_class_idx)

            if i in cached_outputs:
                cached_outputs[i] = x

        # Concatenate predictions from each scale.
        bbox_xywhs = torch.cat(bbox_list, dim=0)
        max_class_idx = torch.cat(max_class_idx_list)

        # Scale bbox w and h based on training width/height from net info.
        train_wh = torch.tensor(
            [self.net_info["width"], self.net_info["height"]],
            device=self.device
        )
        bbox_xywhs[:, 2:4].div_(train_wh)

        return {
            "bbox_xywhs": bbox_xywhs,
            "max_class_idx": max_class_idx,
        }


    def load_weights(self, weights_path):
        """
        Refer to
        https://blog.paperspace.com/how-to-implement-a-yolo-v3-object-detector-from-scratch-in-pytorch-part-3/

        Args:
            weights_path (str): Path to Darknet .weights file.
        """
        with open(weights_path, "rb") as f:
            header = np.fromfile(f, dtype=np.int32, count=5)
            self.header = header
            weights = np.fromfile(f, dtype=np.float32)

            # Index (pointer) to position in weights array.
            p = 0

            for i, (block, module) in enumerate(zip(self.blocks, self.modules_)):
                # Only "convolutional" blocks have weights.
                if block["type"] == "convolutional":
                    conv = module[0]

                    if "batch_normalize" in block and block["batch_normalize"]:
                        # Convolutional blocks with batch norm have weights
                        # stored in the following order: bn biases, bn weights,
                        # bn running mean, bn running var, conv weights.

                        bn = module[1]
                        bn_len = bn.bias.numel()

                        bn_biases = torch.from_numpy(weights[p:p + bn_len])
                        bn.bias.data.copy_(bn_biases.view_as(bn.bias.data))
                        p += bn_len

                        bn_weights = torch.from_numpy(weights[p:p + bn_len])
                        bn.weight.data.copy_(bn_weights.view_as(bn.weight.data))
                        p += bn_len

                        bn_running_mean = torch.from_numpy(weights[p:p + bn_len])
                        bn.running_mean.copy_(
                            bn_running_mean.view_as(bn.running_mean)
                        )
                        p += bn_len

                        bn_running_var = torch.from_numpy(weights[p:p + bn_len])
                        bn.running_var.copy_(
                            bn_running_var.view_as(bn.running_var)
                        )
                        p += bn_len

                    else:
                        # Convolutional blocks without batch norm have weights
                        # stored in the following order: conv biases, conv weights.
                        num_conv_biases = conv.bias.numel()
                        conv_biases = torch.from_numpy(weights[p:p + num_conv_biases])
                        conv.bias.data.copy_(conv_biases.view_as(conv.bias.data))
                        p += num_conv_biases

                    num_weights = conv.weight.numel()
                    conv_weights = torch.from_numpy(weights[p:p + num_weights])
                    conv.weight.data.copy_(conv_weights.view_as(conv.weight.data))
                    p += num_weights
        return self
