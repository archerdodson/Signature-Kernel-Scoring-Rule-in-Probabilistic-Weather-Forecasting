import functools
import logging
from typing import Union

import einops
import numpy as np
import torch
import torch.autograd as autograd
import torch.nn as nn
import torch.nn.functional as F
from torch import broadcast_tensors
from torch.distributions import normal, laplace, cauchy
from torch.utils.data import Dataset
from torchtyping import TensorType, patch_typeguard
from tqdm import tqdm

from src.unet_utils import pad_to_shape, First2D, Center2D, Decoder2D, Encoder2D, Last2D

patch_typeguard()  # use before @typechecked


def createFCNN(input_size, output_size, hidden_sizes=None, nonlinearity=None, nonlinearity_last_layer=False,
               batch_norm=False, batch_norm_last_layer=False, affine_batch_norm=True,
               batch_norm_last_layer_momentum=0.1, add_input_at_the_end=False, unsqueeze_output=False):
    """Function returning a fully connected neural network class with a given input and output size, and optionally
    given hidden layer sizes (if these are not given, they are determined from the input and output size with some
    expression.

    In order to instantiate the network, you need to write: createDefaultNN(input_size, output_size)() as the function
    returns a class, and () is needed to instantiate an object.

    Note that the nonlinearity here is as an object or a functional, not a class, eg:
        nonlinearity =  nn.Softplus()
    or:
        nonlinearity =  nn.functional.softplus
    """

    class FCNN(nn.Module):
        """Neural network class with sizes determined by the upper level variables."""

        def __init__(self):
            super(FCNN, self).__init__()
            # put some fully connected layers:

            if hidden_sizes is not None and len(hidden_sizes) == 0:
                # it is effectively a linear network
                self.fc_in = nn.Linear(input_size, output_size)

            else:
                if hidden_sizes is None:
                    # then set some default values for the hidden layers sizes; is this parametrization reasonable?
                    hidden_sizes_list = [int(input_size * 1.5), int(input_size * 0.75 + output_size * 3),
                                         int(output_size * 5)]

                else:
                    hidden_sizes_list = hidden_sizes

                self.fc_in = nn.Linear(input_size, hidden_sizes_list[0])

                # define now the hidden layers
                self.fc_hidden = nn.ModuleList()
                for i in range(len(hidden_sizes_list) - 1):
                    self.fc_hidden.append(nn.Linear(hidden_sizes_list[i], hidden_sizes_list[i + 1]))
                self.fc_out = nn.Linear(hidden_sizes_list[-1], output_size)

                # define the batch_norm:
                if batch_norm:
                    self.bn_in = nn.BatchNorm1d(hidden_sizes_list[0])
                    self.bn_hidden = nn.ModuleList()
                    for i in range(len(hidden_sizes_list) - 1):
                        self.bn_hidden.append(nn.BatchNorm1d(hidden_sizes_list[i + 1]))
                if batch_norm_last_layer:
                    self.bn_out = nn.BatchNorm1d(output_size, affine=affine_batch_norm,
                                                 momentum=batch_norm_last_layer_momentum)

            self.nonlinearity_fcn = F.relu if nonlinearity is None else nonlinearity

        def forward(self, x: Union[
            TensorType["batch_size", "data_size"], TensorType["batch_size", "window_size", "data_size"]]) -> \
                Union[TensorType["batch_size", "output_size"], TensorType["batch_size", 1, "output_size"]]:

            if x.ndim == 3:
                x = x.reshape(x.shape[0], -1)

            if add_input_at_the_end:
                x_0 = x.clone().detach()  # this may not be super efficient, but for now it is fine

            if not hasattr(self,
                           "fc_hidden"):  # it means that hidden sizes was provided and the length of the list was 0
                x = self.fc_in(x)
                if nonlinearity_last_layer:
                    x = self.nonlinearity_fcn(x)
                return x

            x = self.nonlinearity_fcn(self.fc_in(x))
            if batch_norm:
                x = self.bn_in(x)
            for i in range(len(self.fc_hidden)):
                x = self.nonlinearity_fcn(self.fc_hidden[i](x))
                if batch_norm:
                    x = self.bn_hidden[i](x)

            x = self.fc_out(x)
            if add_input_at_the_end:
                x += x_0  # add input (before batch_norm)
            if batch_norm_last_layer:
                x = self.bn_out(x)
            if nonlinearity_last_layer:
                x = self.nonlinearity_fcn(x)

            if unsqueeze_output:
                x = x.unsqueeze(1)
            return x

    return FCNN


def createGenerativeFCNN(input_size, output_size, hidden_sizes=None, nonlinearity=None):
    """Function returning a fully connected neural network class with a given input and output size, and optionally
    given hidden layer sizes (if these are not given, they are determined from the input and output size with some
    expression. With respect to the one above, the NN here has two inputs,
    which are the context x and the auxiliary variable z in the case of this being used for a generative model.

    In order to instantiate the network, you need to write: createGenerativeFCNN(input_size, output_size)() as the function
    returns a class, and () is needed to instantiate an object.

    Note that the nonlinearity here is as an object or a functional, not a class, eg:
        nonlinearity =  nn.Softplus()
    or:
        nonlinearity =  nn.functional.softplus
    """

    class GenerativeFCNN(nn.Module):
        """Neural network class with sizes determined by the upper level variables."""

        def __init__(self):
            super(GenerativeFCNN, self).__init__()
            # put some fully connected layers:

            if hidden_sizes is not None and len(hidden_sizes) == 0:
                # it is effectively a linear network
                self.fc_in = nn.Linear(input_size, output_size)

            else:
                if hidden_sizes is None:
                    # then set some default values for the hidden layers sizes; is this parametrization reasonable?
                    hidden_sizes_list = [int(input_size * 1.5), int(input_size * 0.75 + output_size * 3),
                                         int(output_size * 5)]

                else:
                    hidden_sizes_list = hidden_sizes

                self.fc_in = nn.Linear(input_size, hidden_sizes_list[0])

                # define now the hidden layers
                self.fc_hidden = nn.ModuleList()
                for i in range(len(hidden_sizes_list) - 1):
                    self.fc_hidden.append(nn.Linear(hidden_sizes_list[i], hidden_sizes_list[i + 1]))
                self.fc_out = nn.Linear(hidden_sizes_list[-1], output_size)

            self.nonlinearity_fcn = F.relu if nonlinearity is None else nonlinearity

        def forward(self, context: TensorType["batch_size", "window_size", "data_size"],
                    z: TensorType["batch_size", "number_generations", "size_auxiliary_variable"]) \
                -> TensorType["batch_size", "number_generations", "output_size"]:
            # this network just flattens the context and concatenates it to the auxiliary variable, for each possible
            # auxiliary variable for each batch element, and then uses a FCNN.
            # input size of the FCNN must be equal to window_size * data_size + size_auxiliary_variable

            batch_size, window_size, data_size = context.shape
            batch_size, number_generations, size_auxiliary_variable = z.shape

            # can add argument `output_size=batch_size * number_generations` with torch 1.10
            repeated_context = torch.repeat_interleave(context, repeats=number_generations, dim=0).reshape(
                batch_size, number_generations, window_size * data_size)

            input_tensor = torch.cat((repeated_context, z), dim=-1)

            if not hasattr(self,
                           "fc_hidden"):  # it means that hidden sizes was provided and the length of the list was 0
                input_tensor = self.fc_in(input_tensor)
                return input_tensor

            input_tensor = self.nonlinearity_fcn(self.fc_in(input_tensor))
            for i in range(len(self.fc_hidden)):
                input_tensor = self.nonlinearity_fcn(self.fc_hidden[i](input_tensor))

            return self.fc_out(input_tensor)

    return GenerativeFCNN


def createCriticFCNN(input_size, hidden_sizes=None, nonlinearity=None, end_sigmoid=True):
    """Function returning a fully connected neural network class with a given input and output size, and optionally
    given hidden layer sizes (if these are not given, they are determined from the input and output size with some
    expression. With respect to the one above, the NN here has two inputs,
    which are the context x and the auxiliary variable z in the case of this being used for a Critic model.

    In order to instantiate the network, you need to write: createCriticFCNN(input_size, output_size)() as the function
    returns a class, and () is needed to instantiate an object.

    Note that the nonlinearity here is as an object or a functional, not a class, eg:
        nonlinearity =  nn.Softplus()
    or:
        nonlinearity =  nn.functional.softplus

    Output size is always 1 here. Additionally, a sigmoid is applied at the end so that the output is between 0 and 1.
    """

    class CriticFCNN(nn.Module):
        """Neural network class with sizes determined by the upper level variables."""

        def __init__(self):
            super(CriticFCNN, self).__init__()
            # put some fully connected layers:

            if hidden_sizes is not None and len(hidden_sizes) == 0:
                # it is effectively a linear network
                self.fc_in = nn.Linear(input_size, 1)

            else:
                if hidden_sizes is None:
                    # then set some default values for the hidden layers sizes; is this parametrization reasonable?
                    hidden_sizes_list = [int(input_size * 1.5), int(input_size * 0.75 + 3), int(5)]

                else:
                    hidden_sizes_list = hidden_sizes

                self.fc_in = nn.Linear(input_size, hidden_sizes_list[0])

                # define now the hidden layers
                self.fc_hidden = nn.ModuleList()
                for i in range(len(hidden_sizes_list) - 1):
                    self.fc_hidden.append(nn.Linear(hidden_sizes_list[i], hidden_sizes_list[i + 1]))
                self.fc_out = nn.Linear(hidden_sizes_list[-1], 1)

            self.nonlinearity_fcn = F.relu if nonlinearity is None else nonlinearity

        def forward(self, y: TensorType["batch_size", "data_size"],
                    context: TensorType["batch_size", "window_size", "context_size"]) -> TensorType["batch_size", 1]:
            # this network just flattens the context and concatenates it to the realization y (which is either predicted
            # by the generator or real), and then uses a FCNN.
            # input size of the FCNN must be equal to window_size * context_size + data_size

            # can add argument `output_size=batch_size * number_generations` with torch 1.10
            input_tensor = torch.cat((y, context.flatten(start_dim=1)), dim=-1)

            if not hasattr(self,
                           "fc_hidden"):  # it means that hidden sizes was provided and the length of the list was 0
                input_tensor = self.fc_in(input_tensor)
                return input_tensor

            input_tensor = self.nonlinearity_fcn(self.fc_in(input_tensor))
            for i in range(len(self.fc_hidden)):
                input_tensor = self.nonlinearity_fcn(self.fc_hidden[i](input_tensor))

            result = self.fc_out(input_tensor)
            if end_sigmoid:
                result = torch.sigmoid(result)  # need to return value between 0 and 1

            return result

    return CriticFCNN


def createGRUNN(data_size, gru_hidden_size, output_size, hidden_sizes=None, gru_layers=3, nonlinearity=None):
    """Function returning a recurrent neural network with a given input and output size, and optionally
    given hidden layer sizes (if these are not given, they are determined from the input and output size with some
    expression. The NN is a single input: the context x.

    In order to instantiate the network, you need to write: createGRUNN(input_size, output_size)() as the function
    returns a class, and () is needed to instantiate an object.

    Note that the nonlinearity here is as an object or a functional, not a class, eg:
        nonlinearity =  nn.Softplus()
    or:
        nonlinearity =  nn.functional.softplus
    """

    class GRUNN(nn.Module):
        """Neural network class with sizes determined by the upper level variables."""

        def __init__(self):
            super(GRUNN, self).__init__()

            # GRU layer:
            self.gru = nn.GRU(data_size, gru_hidden_size, gru_layers, batch_first=True)

            # put some fully connected layers after the gru
            fc_in_size = gru_hidden_size

            # instantiate a generativeFCNN:
            self.fc_nn = createFCNN(fc_in_size, output_size, hidden_sizes, nonlinearity, unsqueeze_output=True)()

        def forward(self, x: TensorType["batch_size", "window_size", "data_size"]) \
                -> TensorType["batch_size", 1, "output_size"]:
            gru_out, _ = self.gru(x)

            # this has shape [batch_size, window_size, gru_hidden_size]; take only the last temporal element:
            gru_out = gru_out[:, -1, :].unsqueeze(1)

            # now apply the FC generative net:
            out = self.fc_nn(gru_out)
            return out

    return GRUNN


def createGenerativeGRUNN(data_size, gru_hidden_size, noise_size, output_size, hidden_sizes=None, gru_layers=3,
                          nonlinearity=None):
    """Function returning a recurrent neural network with a given input and output size, and optionally
    given hidden layer sizes (if these are not given, they are determined from the input and output size with some
    expression). The NN here has two inputs,
    which are the context x and the auxiliary variable z in the case of this being used for a generative model.

    In order to instantiate the network, you need to write: createGenerativeGRUNN(input_size, output_size)() as the function
    returns a class, and () is needed to instantiate an object.

    Note that the nonlinearity here is as an object or a functional, not a class, eg:
        nonlinearity =  nn.Softplus()
    or:
        nonlinearity =  nn.functional.softplus
    """

    class GenerativeGRUNN(nn.Module):
        """Neural network class with sizes determined by the upper level variables."""

        def __init__(self):
            super(GenerativeGRUNN, self).__init__()

            # GRU layer:
            self.gru = nn.GRU(data_size, gru_hidden_size, gru_layers, batch_first=True)

            # put some fully connected layers after the gru
            fc_in_size = gru_hidden_size + noise_size

            # instantiate a generativeFCNN:
            self.fc_nn = createGenerativeFCNN(fc_in_size, output_size, hidden_sizes, nonlinearity)()

        def forward(self, context: TensorType["batch_size", "window_size", "data_size"],
                    z: TensorType["batch_size", "number_generations", "size_auxiliary_variable"]) \
                -> TensorType["batch_size", "number_generations", "output_size"]:
            gru_out, _ = self.gru(context)

            # this has shape [batch_size, window_size, gru_hidden_size]; take only the last temporal element:
            gru_out = gru_out[:, -1, :].unsqueeze(1)

            # now apply the FC generative net:
            out = self.fc_nn(gru_out, z)
            return out

    return GenerativeGRUNN


def createCriticGRUNN(data_size, gru_hidden_size, hidden_sizes=None, gru_layers=3, nonlinearity=None, end_sigmoid=True):
    """Function returning a fully connected neural network class with a given input and output size, and optionally
    given hidden layer sizes (if these are not given, they are determined from the input and output size with some
    expression). The NN here has two inputs,
    which are the context x and the auxiliary variable z in the case of this being used for a Critic model.

    In order to instantiate the network, you need to write: createCriticGRUNN(input_size, output_size)() as the function
    returns a class, and () is needed to instantiate an object.

    Note that the nonlinearity here is as an object or a functional, not a class, eg:
        nonlinearity =  nn.Softplus()
    or:
        nonlinearity =  nn.functional.softplus

    Output size is always 1 here. Additionally, a sigmoid is applied at the end so that the output is between 0 and 1.
    """

    class CriticGRUNN(nn.Module):
        """Neural network class with sizes determined by the upper level variables."""

        def __init__(self):
            super(CriticGRUNN, self).__init__()

            # GRU layer:
            self.gru = nn.GRU(data_size, gru_hidden_size, gru_layers, batch_first=True)

            # put some fully connected layers after the gru
            self.fc_nn = createCriticFCNN(gru_hidden_size + data_size, hidden_sizes, nonlinearity, end_sigmoid)()

        def forward(self, y: TensorType["batch_size", "data_size"],
                    context: TensorType["batch_size", "window_size", "data_size"]) -> TensorType["batch_size", 1]:
            gru_out, _ = self.gru(context)

            # this has shape [batch_size, window_size, gru_hidden_size]; take only the last temporal element:
            gru_out = gru_out[:, -1, :].unsqueeze(1)

            # now apply the FC generative net:
            out = self.fc_nn(y, gru_out)
            return out

    return CriticGRUNN


class LayerNormMine(nn.Module):
    def __init__(self, *args, **kwargs):
        super(LayerNormMine, self).__init__()

    def forward(self, x: TensorType["batch_size", "C", "H", "W"]):
        # assume x is 4 dimensional, and we apply LayerNorm on the last 3 dimensions:
        return torch.nn.functional.layer_norm(x, x.shape[1:4], weight=None, bias=None, eps=1e-05)


class DummyLayer(nn.Module):
    """"""

    def __init__(self, *args, **kwargs):
        super(DummyLayer, self).__init__()

    def forward(self, x):
        return x


class UNet2D(nn.Module):
    def __init__(self, in_channels=6, out_channels=1, conv_depths=(32, 64, 128, 256),
                 noise_method="sum", number_generations_per_forward_call=1, dropout_level=0.2,
                 ratio_noise_size_to_channel_depth=1):
        """[summary]
        noise_method="dropout" as in Isola et al. 2017.

        Args:
            in_channels (int, optional): [This is the number of weather features present in input]. Defaults to 6.
            out_channels (int, optional): [This is the number of output variables]. Defaults to 1.
            conv_depths (tuple, optional): [This is the depth of the intermediary convolutions]. Defaults to (32, 64, 128, 256).
            dropout_level (False,float, optional): [Dropout to be used in model]
            noise_method (str, optional): []. Defaults to None. Choices ['sum', 'dropout', 'concat', 'no noise']
            ratio_noise_size_to_channel_depth (float, optional): [Proportion of additional channels to allocate to
             noise in the bottleneck, when noise_method="concat"]. Defaults to 1

        Adapted from https://github.com/cosmic-cortex/pytorch-UNet
        """
        assert len(conv_depths) > 2, 'conv_depths must have at least 3 members'
        if noise_method not in ['sum', 'dropout', 'concat', 'no noise']:
            raise NotImplementedError("The chosen noise method is unavailable")
        self.noise_method = noise_method

        super(UNet2D, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels

        # defining encoder layers
        encoder_layers = []
        encoder_layers.append(First2D(in_channels, conv_depths[0], conv_depths[0]))
        encoder_layers.extend([Encoder2D(conv_depths[i], conv_depths[i + 1], conv_depths[i + 1])
                               for i in range(len(conv_depths) - 2)])

        # defining decoder layers
        decoder_layers = []

        if self.noise_method == "concat":

            decoder_layers.append(Decoder2D(
                in_channels=(2 + ratio_noise_size_to_channel_depth) * conv_depths[-2],
                middle_channels=2 * conv_depths[-3],
                out_channels=2 * conv_depths[-3],
                deconv_channels=conv_depths[-3]))

            decoder_layers.extend(
                [Decoder2D(2 * conv_depths[i + 1], 2 * conv_depths[i], 2 * conv_depths[i], conv_depths[i])
                 for i in reversed(range(len(conv_depths) - 3))])

        else:  # abilitate dropout if needed
            decoder_layers.extend(
                [Decoder2D(2 * conv_depths[i + 1], 2 * conv_depths[i], 2 * conv_depths[i], conv_depths[i],
                           dropout=dropout_level if noise_method == "dropout" else 0)
                 for i in reversed(range(len(conv_depths) - 2))])

        decoder_layers.append(Last2D(conv_depths[1], conv_depths[0], out_channels))

        # encoder, center and decoder layers
        self.encoder_layers = nn.Sequential(*encoder_layers)
        self.center = Center2D(conv_depths[-2], conv_depths[-1], conv_depths[-1], conv_depths[-2],
                               dropout=dropout_level if noise_method == "dropout" else 0)
        self.decoder_layers = nn.Sequential(*decoder_layers)

        self.conv_depths = conv_depths
        self.number_generations_per_forward_call = number_generations_per_forward_call
        self.ratio_noise_size_to_channel_depth = ratio_noise_size_to_channel_depth

    def forward(self, x: TensorType["batch_size", "height", "width", "fields"],
                z: TensorType["batch_size", "number_generations", -1, -1, -1] = None) -> TensorType[
        "batch_size", "number_generations", "height", "width", "fields"]:
        """Forward pass for UNET

        Note: When we add noise to UNET, we add it in the decoder steps not in the encoder steps.
            naive reasoning -> If we add it in the encoder steps it may become negligible by the time we reach the encoder steps?? [Need to explore different approaches]

        Args:
            x ([torch.Tensor]): [Input variables]
            z ([torch.Tensor]): [The random noise added to the model in the Generative or Gan Setting].
            return_all (bool, optional): [set True return all intermediary hidden representations ]. Defaults to False.

        TODO make this code cleaner, can use a separate forward method for the dropout setting, so that it does not
         need to be wrapped by the net that generates the latent variables and can not take z as input.

        Returns:
            [type]: [description]
        """
        if x.dim() == 5:
        # Remove the trivial channel dimension (last dim) to get [B, ObsWindow, H, W]
            x = x.squeeze(-1)
            # Permute to move the observation window into the 'fields' (channel) position:
            # from [B, ObsWindow, H, W] to [B, H, W, ObsWindow]
            x = x.permute(0, 2, 3, 1)

        # transpose the axis, as the following code needs (Batch, Channels, Height, Width)
        x = x.permute(0, 3, 1, 2)

        # encoding stage: do it only once for each batch sample
        x_enc = [x]
        # pass input through encoder
        for enc_layer in self.encoder_layers:
            x_enc.append(enc_layer(x_enc[-1]))

        # if using the dropout method, need to pass that through the decoder part several times:
        if self.noise_method == "dropout":
            out = torch.stack(
                [self._forward_decoder(x_enc, z) for i in range(self.number_generations_per_forward_call)], dim=1)
        else:  # with summed/concatenated noise or with no noise
            out = self._forward_decoder(x_enc, z).reshape(x.shape[0], -1, self.out_channels, x.shape[2], x.shape[3])
        return out.permute(0, 1, 3, 4, 2)

    def _forward_decoder(self, x_enc, z):
        # x_dec = [self.center(x_enc[-1])]
        x_dec = self.center(x_enc[-1])

        if self.noise_method == "sum":
            n_generations = z.shape[1]
            # Adding noise in the bottleneck.
            # x_dec[0] = (x_dec[0].unsqueeze(1) + z).flatten(start_dim=0, end_dim=1)
            x_dec = (x_dec.unsqueeze(1) + z).flatten(start_dim=0, end_dim=1)
        elif self.noise_method == "concat":
            n_generations = z.shape[1]
            # Concat on dim -3 since our tensors have shape (..., C, H, W)
            # NOTE: now z must have shape (B, E, C, H, W), where E is ensemble size (number of forward generations)
            x_dec_exp = x_dec.unsqueeze(1).expand(-1, n_generations, -1, -1, -1)
            x_dec = torch.cat([x_dec_exp, z], dim=-3).flatten(start_dim=0, end_dim=1)  # shape (..., c)

        # pass through decoder
        for dec_layer_idx, dec_layer in enumerate(self.decoder_layers):
            x_opposite = x_enc[-1 - dec_layer_idx]
            if self.noise_method in ["sum", "concat"]:
                x_opposite = torch.repeat_interleave(x_opposite, repeats=n_generations, dim=0)
            # here it concatenates with the encoded features along the channel dimension.
            # x_cat = torch.cat([pad_to_shape(x_dec[-1], x_opposite.shape), x_opposite], dim=1)
            # x_dec.append(dec_layer(x_cat))
            x_cat = torch.cat([pad_to_shape(x_dec, x_opposite.shape), x_opposite], dim=1)
            x_dec = dec_layer(x_cat)

        # return x_dec[-1]
        return x_dec

    def calculate_downsampling_factor(self):
        # In bottleneck, we have to calculate the size of our latent representation at the bottleneck. That is the size
        # of the original image times some downsampling factor due to the number of convolutional layers. Additionally,
        # the number of channels is the number of channels of last encoder layer.
        if self.noise_method in ["sum", "concat"]:
            # Downsampling occurs in every encoder layer excluding the First encoder layer
            # Each Time downsample factor is 2
            downsample_factor = 2 ** (len(self.encoder_layers) - 1)
            n_channels = self.conv_depths[-2]  # size of last encoder layer
            if self.noise_method == "concat":
                n_channels = int(n_channels * self.ratio_noise_size_to_channel_depth)
        else:
            raise NotImplementedError

        return downsample_factor, n_channels


class PatchGANDiscriminator(nn.Module):
    """Defines a PatchGAN discriminator

        Adapted from https://github.com/junyanz/pytorch-CycleGAN-and-pix2pix/
    """

    def __init__(self, in_channels, first_layer_filters=32, n_layers=3, norm_layer=nn.BatchNorm2d, end_sigmoid=True):
        """Construct a PatchGAN discriminator
        Parameters:
            in_channels (int)  -- the number of channels in input images
            first_layer_filters (int)       -- the number of filters in the first conv layer
            n_layers (int)  -- the number of conv layers in the discriminator
            norm_layer      -- normalization layer

            NOTE: prev research found that using a receptive field size of 70 given input of 256 tended to produce the best tradeoff between speed and results
                # currently the receptive field size
        """
        super(PatchGANDiscriminator, self).__init__()
        if type(norm_layer) == functools.partial:  # no need to use bias as BatchNorm2d has affine parameters
            use_bias = norm_layer.func == nn.InstanceNorm2d
        else:
            use_bias = norm_layer == nn.InstanceNorm2d

        kw = 2
        padw = 1
        sequence = [nn.Conv2d(in_channels, first_layer_filters, kernel_size=kw, stride=2, padding=padw),
                    nn.LeakyReLU(0.2, True)]
        nf_mult = 1  # number filter multiple
        nf_mult_prev = 1
        for n in range(1, n_layers):  # gradually increase the number of filters in each layer
            nf_mult_prev = nf_mult
            nf_mult = min(2 ** n, 8)
            sequence += [
                nn.Conv2d(first_layer_filters * nf_mult_prev, first_layer_filters * nf_mult, kernel_size=kw, stride=2,
                          padding=padw, bias=use_bias),
                norm_layer(first_layer_filters * nf_mult),
                nn.LeakyReLU(0.2, True)
            ]

        nf_mult_prev = nf_mult
        nf_mult = min(2 ** n_layers, 8)
        sequence += [
            nn.Conv2d(first_layer_filters * nf_mult_prev, first_layer_filters * nf_mult, kernel_size=kw, stride=1,
                      padding=padw, bias=use_bias),
            norm_layer(first_layer_filters * nf_mult),
            nn.LeakyReLU(0.2, True)
        ]

        sequence += [nn.Conv2d(first_layer_filters * nf_mult, 1, kernel_size=kw, stride=1,
                               padding=padw)]  # output 1 channel prediction map
        if end_sigmoid:
            sequence += [nn.Sigmoid()]
        self.model = nn.Sequential(*sequence)

    def forward(self, target: TensorType["batch_size", "height", "width", "fields_target"],
                context: TensorType["batch_size", "window_size", "height", "width", "fields_context"]) -> TensorType[
        "batch_size", "height_reduced", "width_reduced", 1]:
        """Standard forward.
        
        """
        # for the moment, we assume to have a single generation per batch element
        # if target.ndim == 5:
        #     num_gen = target.shape[1]
        #     # flattening window dimension with channel dimension does not seem correct!
        #     # target = einops.rearrange(target,'b window c h w -> b (window c) h w' ) # (N, window*C, H, W)
        # else:
        #     num_gen = 1

        # In the case of multiple outputs per input, we need to produce 
        target = einops.rearrange(target, '... h w c -> ... c h w')  # permuting channels dimension
        context = einops.rearrange(context, '... h w c -> ... c h w')

        # flattening window dimension with channel dimension
        context = einops.rearrange(context, 'b window c h w -> b (window c) h w')  # (N, window*C, H, W)

        # Repeating elements in batch of the context, in case there were multiple generated samples in the batch
        # context = einops.repeat(context, 'b c1 h w -> b (c1 num_gen) h w', num_gen=num_gen)

        # Concatenating in channel dimensions
        input = torch.cat([context, target], dim=1)

        output = self.model(input)  # (batch_size, 1, H, W )

        # Unpacking generation_samples from batch dimension and moving channel dimension to end
        # output = einops.rearrange( output, '(N num_gen) C H W -> N num_gen H W C', num_gen=num_gen)
        output = einops.rearrange(output, 'b C H W -> b H W C')

        # averaging the loss for each set of generation_samples
        # output = einops.reduce( output, 'N num_gen H W C -> N 1 H W C', 'mean' )

        return output


class ConditionalGenerativeModel(nn.Module):
    """This is a class wrapping a net which takes as an input a conditioning variable and an auxiliary variable,
    concatenates then and then feeds them through the NN. The auxiliary variable generation is done whenever the
    forward method is called."""

    def __init__(self, net, size_auxiliary_variable: Union[int, torch.Size], number_generations_per_forward_call: int,
                 seed: int = 42, base_measure: str = "normal"):
        super(ConditionalGenerativeModel, self).__init__()
        self.net = net  # net has to be able to take input size `size_auxiliary_variable + dim(x)`
        if isinstance(size_auxiliary_variable, int):
            size_auxiliary_variable = torch.Size([size_auxiliary_variable])
        self.size_auxiliary_variable = size_auxiliary_variable
        self.number_generations_per_forward_call = number_generations_per_forward_call
        # set the seed of the random number generator for ensuring reproducibility:
        torch.random.manual_seed(seed)

        self.seed = seed 
        #Seed is 1:k, plus an iteration for path length


        distribution_dict = {"normal": normal.Normal, "laplace": laplace.Laplace, "cauchy": cauchy.Cauchy}
        if base_measure not in distribution_dict.keys():
            raise NotImplementedError("Base measure not available")
        self.distribution = distribution_dict[base_measure](loc=torch.tensor([0.0]), scale=torch.tensor([1.0]))
        #Add in third parameter seed 

    # def resetseed(self):
    #     self.seed = 0
    #     torch.random.manual_seed(self.seed)

    def forward(self, context: Union[TensorType["batch_size", "window_size", "data_size"], TensorType[
        "batch_size", "window_size", "height", "width", "fields"]],
                number_generations: int = None) -> Union[
        TensorType["batch_size", "number_generations", "data_size"], TensorType[
            "batch_size", "number_generations", "height", "width", "fields"]]:
        """This returns a stacked torch tensor for outputs. For now, I use different random auxiliary variables for
            each element in the batch size, but in principle you could take the same."""

        # you basically need to generate self.number_generations_per_forward_call forward simulations with a different
        # auxiliary variable z, all with the same conditioning variable x, for each conditioning variable x:
        if number_generations is None:
            number_generations = self.number_generations_per_forward_call

        ##My code
        # if context.ndim == 2:
        #     window_size, data_size = context.shape
        #     batch_size = 1
        #     print('trigger2')
        #print(context.ndim)

        if context.ndim == 3:
            batch_size, window_size, data_size = context.shape
        elif context.ndim == 5:
            batch_size, window_size, height, width, n_fields = context.shape
            # assume that the window_size is 1 for the moment now:
            # if window_size != 1:
            #     raise NotImplementedError("We do not yet implement UNet for observation windows larger than 1")
            # context = context.squeeze(1)
        else:
            raise NotImplementedError

        #print("Running forward")
        #print("Seed +1")
        # self.seed = self.seed + 1 
        #print(self.seed)
        #torch.random.manual_seed(self.seed)

        # generate the auxiliary variables; use different noise for each batch element
        z = self.distribution.sample(torch.Size([batch_size, number_generations]) + self.size_auxiliary_variable).to(
            device="cuda" if next(self.parameters()).is_cuda else "cpu").squeeze(-1)
        
        #Add prediction length here
        #

        return self.net(context, z)


class DiscardWindowSizeDim(nn.Module):

    def __init__(self, net):
        super(DiscardWindowSizeDim, self).__init__()
        self.net = net

    def forward(self, context: Union[TensorType["batch_size", "window_size": 1, "data_size"], TensorType[
                                                                                              "batch_size",
                                                                                              "window_size": 1,
                                                                                              "height", "width",
                                                                                              "fields"]]) -> Union[
        TensorType["batch_size", "number_generations", "data_size"], TensorType[
            "batch_size", "number_generations", "height", "width", "fields"]]:
        context = context.squeeze(1)

        return self.net(context)


class DiscardNumberGenerationsInOutput(nn.Module):
    """This is a helper class that discards the number of generations in the output."""
    def __init__(self, net):
        super(DiscardNumberGenerationsInOutput, self).__init__()
        self.net = net

    def forward(self, context: Union[TensorType["batch_size", "window_size", "data_size"], TensorType[
        "batch_size",
        "window_size",
        "height", "width",
        "fields"]]) -> Union[
        TensorType["batch_size", "data_size"], TensorType["batch_size", "height", "width", "fields"]]:
        return self.net(context).squeeze(1)


class InputTargetDataset(Dataset):
    """A dataset class that consists of pairs of input-target variables, in which simulations has shape
    (n_samples, n_features), and targets contains the ground truth with shape (n_samples, target_size).
    Note that n_features could also have more than one dimension here. """

    def __init__(self, input, target, device):
        """
        Parameters:

        simulations: (n_samples,  n_features)
        target: (n_samples, target_size)
        """
        if input.shape[0] != target.shape[0]:
            raise RuntimeError("The number of inputs must be the same as the number of target.")

        if isinstance(input, np.ndarray):
            self.input = torch.from_numpy(input.astype("float32")).to(device)
        else:
            self.input = input.to(device)
        if isinstance(target, np.ndarray):
            self.target = torch.from_numpy(target.astype("float32")).to(device)
        else:
            self.target = target.to(device)

    def __getitem__(self, index):
        """Return the required sample along with the ground truth parameter."""
        return self.input[index], self.target[index]

    def __len__(self):
        return self.target.shape[0]


def fit(train_loader, model, loss_fn, optimizer, scheduler, n_epochs, cuda, val_loader=None, early_stopping=False,
        epochs_early_stopping_interval=1, start_epoch_early_stopping=10, start_epoch_training=0, use_tqdm=True,
        train_loss_list=None, test_loss_list=None, prediction_length=None, scaling_mean = None, scaling_std = None, val_mean = None, val_std = None):
    """
    Basic function to train a neural network given a train_loader, a loss function and an optimizer.

    Loaders, model, loss function and metrics should work together for a given task,
    i.e. The model should be able to process data output of loaders,
    loss function should process target output of loaders and outputs from the model

    Adapted from https://github.com/adambielski/siamese-triplet
    """

    logger = logging.getLogger("NN Trainer")
    if train_loss_list is None:
        train_loss_list = []
    if val_loader is not None:
        if test_loss_list is None:
            test_loss_list = []
            if early_stopping:
                early_stopping_loss_list = []  # list of losses used for early stopping
        else:
            early_stopping_loss_list = [test_loss_list[-1]]
    else:
        test_loss_list = None
    if early_stopping and val_loader is None:
        raise RuntimeError("You cannot perform early stopping if a validation loader is not provided to the training "
                           "routine")

    for epoch in range(0, start_epoch_training):
        scheduler.step()

    for epoch in tqdm(range(start_epoch_training, n_epochs), disable=not use_tqdm):
        # Train stage
        if prediction_length is None:
            print("train_loss wrong read")
            train_loss = train_epoch(train_loader, model, loss_fn, optimizer, cuda)
        else:
            #print("train_epoch_longerprediction")
            train_loss =  train_epoch_longerpredictionbatch(train_loader, model, loss_fn, optimizer, cuda, prediction_length,scaling_mean,scaling_std)
            #train_loss = train_epoch_longerpredictionbatch(train_loader, model, loss_fn, optimizer, cuda, prediction_length,scaling_mean,scaling_std)
            #train_epoch_longerprediction
            #val_loader = None  # commneting the validation set for the longer prediction case for now

        train_loss_list.append(train_loss)

        print('Epoch: {}/{}. Train set: Average loss: {:.4f}'.format(epoch + 1, n_epochs, train_loss))

        logger.debug('Epoch: {}/{}. Train set: Average loss: {:.4f}'.format(epoch + 1, n_epochs, train_loss))


        # Validation stage
        if val_loader is not None:
            val_loss = test_epochlongerpredictionbatch(val_loader, model, loss_fn, cuda, prediction_length,val_mean,val_std)
            test_loss_list.append(val_loss)

            print('Epoch: {}/{}. Validation set: Average loss: {:.4f}'.format(epoch + 1, n_epochs, val_loss))
            logger.debug('Epoch: {}/{}. Validation set: Average loss: {:.4f}'.format(epoch + 1, n_epochs, val_loss))

            # early stopping:
            if early_stopping and (epoch + 1) % epochs_early_stopping_interval == 0:
                early_stopping_loss_list.append(val_loss)  # save the previous validation loss. It is actually
                # we need to have at least two saved test losses for performing early stopping (in which case we know
                # we have saved the previous state_dict as well).
                if epoch + 1 >= start_epoch_early_stopping and len(early_stopping_loss_list) > 1:
                    if early_stopping_loss_list[-1] > early_stopping_loss_list[-2]:
                        logger.info("Training has been early stopped at epoch {}.".format(epoch + 1))
                        # reload the previous state dict:
                        model.load_state_dict(net_state_dict)
                        print("yo?")
                        break  # stop training
                # if we did not stop: update the state dict to the next value
                net_state_dict = model.state_dict()

        scheduler.step()

    return train_loss_list, test_loss_list


def train_epoch(train_loader, model, loss_fn, optimizer, cuda):
    """Function implementing the training in one epoch.

    Adapted from https://github.com/adambielski/siamese-triplet
    """
    model.train()
    total_loss = 0

    for batch_idx, (data, target) in enumerate(train_loader):
        target = target if len(target) > 0 else None
        if not type(data) in (tuple, list):
            data = (data,)
        if cuda:
            data = tuple(d.cuda() for d in data)
            if target is not None:
                target = target.cuda()
        
        print('batch_idx:',batch_idx, 'data:',data, 'data0', data[0].shape)

        optimizer.zero_grad()
        outputs = model(*data)

        print('model outputs:',outputs.shape)

        # assert not torch.all(outputs[0][:, 0] == outputs[0][:, 1])

        if type(outputs) not in (tuple, list):
            outputs = (outputs,)

        loss_inputs = outputs

        if target is not None:
            target = (target,)
            loss_inputs += target

        loss_outputs = loss_fn(*loss_inputs)
        loss = loss_outputs[0] if type(loss_outputs) in (tuple, list) else loss_outputs
        total_loss += loss.item()
        loss.backward()
        optimizer.step()

    return total_loss / (batch_idx + 1)  # divide here by the number of elements in the batch.


def test_epoch(val_loader, model, loss_fn, cuda):
    """Function implementing the computation of the validation error, in batches.

    Adapted from https://github.com/adambielski/siamese-triplet
    """
    with torch.no_grad():
        model.eval()
        val_loss = 0
        for batch_idx, (data, target) in enumerate(val_loader):
            target = target if len(target) > 0 else None
            if not type(data) in (tuple, list):
                data = (data,)
            if cuda:
                data = tuple(d.cuda() for d in data)
                if target is not None:
                    target = target.cuda()

            outputs = model(*data)
            # assert not torch.all(outputs[0][:, 0] == outputs[0][:, 1])

            if type(outputs) not in (tuple, list):
                outputs = (outputs,)
            loss_inputs = outputs
            if target is not None:
                target = (target,)
                loss_inputs += target

            loss_outputs = loss_fn(*loss_inputs)
            loss = loss_outputs[0] if type(loss_outputs) in (tuple, list) else loss_outputs
            val_loss += loss.item()

    return val_loss / (batch_idx + 1)  # divide here by the number of elements in the batch.


def fit_adversarial(method, train_loader, generator, critic, optimizer_g, scheduler_g, optimizer_c, scheduler_c,
                    n_epochs, cuda, start_epoch_training=0, critic_steps_every_generator_step=1, use_tqdm=True,
                    train_loss_list_g=None, train_loss_list_c=None, **kwargs):
    """
    Basic function to train a neural network given a train_loader, a loss function and an optimizer.

    Loaders, model, loss function and metrics should work together for a given task,
    i.e. The model should be able to process data output of loaders,
    loss function should process target output of loaders and outputs from the model

    Notice that there is no test set for the GAN case. You do not have in fact a decreasing loss, but a min-max game.

    NB at this moment this assumes that the generator generates one single realization for each value of the
    conditioning variables.

    Additionally, it uses the BCELoss, which corresponds to the standard conditional GAN objective (Jensen-Shannon
    divergence one).
    """
    if method == "GAN":
        batch_function = train_epoch_adversarial
    elif method == "WGAN_GP":
        batch_function = train_epoch_adversarial_wass_GP
    else:
        raise NotImplementedError("The required GAN method is not implemented")

    logger = logging.getLogger("NN Trainer")
    if train_loss_list_g is None:
        train_loss_list_g = []
    if train_loss_list_c is None:
        train_loss_list_c = []
    generator.train()
    critic.train()

    for epoch in range(0, start_epoch_training):
        scheduler_g.step()
        scheduler_c.step()

    for epoch in tqdm(range(start_epoch_training, n_epochs), disable=not use_tqdm):
        train_loss_g, train_loss_c = batch_function(train_loader, generator, critic, optimizer_g, optimizer_c, cuda,
                                                    critic_steps_every_generator_step=critic_steps_every_generator_step,
                                                    **kwargs)
        train_loss_list_g.append(train_loss_g)
        train_loss_list_c.append(train_loss_c)

        logger.debug('Epoch: {}/{}. Train set: Average loss generator: {:.4f}. Average loss critic: {:.4f}'.format(
            epoch + 1, n_epochs, train_loss_g, train_loss_c))

        scheduler_g.step()
        scheduler_c.step()

    generator.eval()
    critic.eval()

    return train_loss_list_g, train_loss_list_c


def train_epoch_adversarial(train_loader, generator, critic, optimizer_g, optimizer_c, cuda,
                            critic_steps_every_generator_step=1):
    """Function implementing the training in one epoch.

    """
    total_loss_g = 0
    total_loss_c = 0
    generator_steps = 0

    for batch_idx, (data, target) in enumerate(train_loader):
        batch_size = len(target)

        target = target if batch_size > 0 else None
        if not type(data) in (tuple, list):
            data = (data,)

        valid = torch.ones(1, requires_grad=False)
        fake = torch.zeros(1, requires_grad=False)

        if cuda:
            data = tuple(d.cuda() for d in data)
            valid = valid.cuda()
            fake = fake.cuda()
            if target is not None:
                target = target.cuda()

        # data is the conditioning variable, target is the actual observation which you try to predict

        # for now assume that the generator generates a single output for each value of the conditioning variables
        generated_target = generator(*data)
        # discard the second dimension, which is the number of generations for a given conditioning variable:
        # NOTE: PatchGANDiscriminator is sample size agnostic code -> could work in uniform manner
        # for number generations>=1
        generated_target = generated_target.squeeze(dim=1)

        # --- train critic ---
        optimizer_c.zero_grad()

        # Loss for real images
        c_real_critic = critic(target, *data)
        c_real_loss = F.binary_cross_entropy(*broadcast_tensors(c_real_critic, valid))
        # Loss for fake images; to avoid backward step twice through the generator, need to detach generated_target
        c_fake_critic = critic(generated_target.detach(), *data)
        c_fake_loss = F.binary_cross_entropy(*broadcast_tensors(c_fake_critic, fake))

        c_loss = c_fake_loss + c_real_loss
        total_loss_c += c_loss.item()

        c_loss.backward()
        optimizer_c.step()

        # --- train generator ---
        # every critic_steps_every_generator_step
        if batch_idx % critic_steps_every_generator_step == critic_steps_every_generator_step - 1:
            optimizer_g.zero_grad()
            # g_loss = loss_fn(critic(generated_target, *data).squeeze(), valid)
            g_critic = critic(generated_target, *data)
            g_loss = F.binary_cross_entropy(*broadcast_tensors(g_critic, valid))

            total_loss_g += g_loss.item()
            generator_steps += 1
            g_loss.backward()
            optimizer_g.step()

    # divide total losses by the number of steps .
    return total_loss_g / (batch_idx + 1), total_loss_c / generator_steps


def compute_gradient_penalty(critic, real_samples, fake_samples, context):
    """Calculates the gradient penalty loss for WGAN GP.

    Adapted from https://github.com/eriklindernoren/PyTorch-GAN/blob/master/implementations/wgan_gp/wgan_gp.py"""
    batch_size = real_samples.size(0)
    # Random weight term for interpolation between real and fake samples
    alpha = torch.rand(batch_size, device=real_samples.device)
    while alpha.dim() < real_samples.dim():  # add dummy dimensions
        alpha = alpha.unsqueeze(-1)
    # Get random interpolation between real and fake samples
    interpolates = (alpha * real_samples + ((1 - alpha) * fake_samples)).requires_grad_(True)
    # assert torch.allclose(real_samples[1]* alpha[1,0] + (1-alpha[1,0])*fake_samples[1],interpolates[1])
    # assert torch.allclose(real_samples[1]* alpha[1,0,0,0] + (1-alpha[1,0,0,0])*fake_samples[1],interpolates[1])
    c_interpolates = critic(interpolates, context)
    if c_interpolates.ndim == 4:
        c_interpolates = c_interpolates.mean(dim=(1, 2))

    # if c_interpolates.ndim == 2:
    # this is used in the computation of the gradients to multiply the batch of critic outputs
    fake = torch.Tensor(batch_size, 1).fill_(1.0).requires_grad_(False).to(real_samples.device)
    # Get gradient w.r.t. interpolates
    gradients = autograd.grad(
        outputs=c_interpolates,
        inputs=interpolates,
        grad_outputs=fake,
        create_graph=True,
        retain_graph=True,
        only_inputs=True,
    )[0]
    gradients = gradients.view(gradients.size(0), -1)
    gradient_penalty = ((gradients.norm(2, dim=1) - 1) ** 2).mean()
    # elif c_interpolates.ndim == 4:
    #     assert c_interpolates.shape[3] == 1
    #     # In this case, the critic is the patchedGAN one -> it outputs a set of values each corresponding to a single
    #     # patch on the original image. Therefore, you would need to loop on the different output elements, and compute
    #     # the gradients and the corresponding penalty for each of them independently. Not super efficient but don't
    #     # know how to do otherwise.
    #
    #     # gradient_penalty = 0
    #     # for i in range(c_interpolates.shape[1]):
    #     #     for j in range(c_interpolates.shape[2]):
    #     #         # this is used in the computation of the gradients to multiply the batch of critic outputs
    #     #         fake = torch.Tensor(batch_size, c_interpolates.shape[1], c_interpolates.shape[2], 1).fill_(
    #     #             0.0).requires_grad_(False).to(real_samples.device)
    #     #         fake[:, i, j, 0] = 1.0
    #     #         # Get gradient w.r.t. interpolates
    #     #         gradients = autograd.grad(
    #     #             outputs=c_interpolates,
    #     #             inputs=interpolates,
    #     #             grad_outputs=fake,
    #     #             create_graph=True,
    #     #             retain_graph=True,
    #     #             only_inputs=True,
    #     #         )[0]
    #     #         gradients = gradients.view(gradients.size(0), -1)
    #     #         gradient_penalty += ((gradients.norm(2, dim=1) - 1) ** 2).mean()
    #
    #     # The loop is however too slow. What we resort to doing is instead selecting a random output component and
    #     # penalizing the gradient for that component only. Not sure how grounded that is, let's try it out.
    #
    #     i = torch.randint(0, c_interpolates.shape[1], (1,))
    #     j = torch.randint(0, c_interpolates.shape[2], (1,))
    #     # this is used in the computation of the gradients to multiply the batch of critic outputs
    #     fake = torch.Tensor(batch_size, c_interpolates.shape[1], c_interpolates.shape[2], 1).fill_(
    #         0.0).requires_grad_(False).to(real_samples.device)
    #     fake[:, i, j, 0] = 1.0
    #     # Get gradient w.r.t. interpolates
    #     gradients = autograd.grad(
    #         outputs=c_interpolates,
    #         inputs=interpolates,
    #         grad_outputs=fake,
    #         create_graph=True,
    #         retain_graph=True,
    #         only_inputs=True,
    #     )[0]
    #     gradients = gradients.view(gradients.size(0), -1)
    #     gradient_penalty = ((gradients.norm(2, dim=1) - 1) ** 2).mean()
    # else:
    #     raise NotImplementedError

    return gradient_penalty


def train_epoch_adversarial_wass_GP(train_loader, generator, critic, optimizer_g, optimizer_c, cuda,
                                    critic_steps_every_generator_step=1, lambda_gp=10):
    """Function implementing the training in one epoch.

    """
    total_loss_g = 0
    total_loss_c = 0
    generator_steps = 0

    for batch_idx, (data, target) in enumerate(train_loader):
        batch_size = len(target)

        target = target if batch_size > 0 else None
        if not type(data) in (tuple, list):
            data = (data,)

        if cuda:
            data = tuple(d.cuda() for d in data)
            if target is not None:
                target = target.cuda()

        # data is the conditioning variable, target is the actual observation which you try to predict

        # for now assume that the generator generates a single output for each value of the conditioning variables
        generated_target = generator(*data)
        # discard the second dimension, which is the number of generations for a given conditioning variable:
        # NOTE: PatchGANDiscriminator is sample size agnostic code -> could work in uniform manner
        # for number generations>=1
        generated_target = generated_target.squeeze(dim=1)

        # --- train critic ---
        optimizer_c.zero_grad()

        # Loss for real images
        c_real_critic = critic(target, *data)
        c_real_loss = -torch.mean(c_real_critic)
        # Loss for fake images; to avoid backward step twice through the generator, need to detach generated_target
        c_fake_critic = critic(generated_target.detach(), *data)
        c_fake_loss = torch.mean(c_fake_critic)

        gradient_penalty = compute_gradient_penalty(critic, target.data, generated_target.data, *data)

        c_loss = c_fake_loss + c_real_loss + lambda_gp * gradient_penalty
        total_loss_c += c_loss.item()

        c_loss.backward()
        optimizer_c.step()

        # --- train generator ---
        # every critic_steps_every_generator_step
        if batch_idx % critic_steps_every_generator_step == critic_steps_every_generator_step - 1:
            optimizer_g.zero_grad()
            # g_loss = loss_fn(critic(generated_target, *data).squeeze(), valid)
            g_critic = critic(generated_target, *data)
            g_loss = - torch.mean(g_critic)

            total_loss_g += g_loss.item()
            generator_steps += 1
            g_loss.backward()
            optimizer_g.step()

    # divide total losses by the number of steps .
    return total_loss_g / (batch_idx + 1), total_loss_c / generator_steps


def get_predictions_and_target(data_loader, model, cuda):
    """Function returning stacked predictions and target from a data_loader.

    Adapted from https://github.com/adambielski/siamese-triplet
    """
    outputs = []
    targets = []
    with torch.no_grad():
        model.eval()
        for data, target in data_loader:
            target = target if len(target) > 0 else None
            if not type(data) in (tuple, list):
                data = (data,)
            if cuda:
                data = tuple(d.cuda() for d in data)
                if target is not None:
                    target = target.cuda()

            outputs.append(model(*data))
            targets.append(target)
    return torch.cat(outputs, dim=0), torch.cat(targets, dim=0)


def get_target(data_loader, cuda):
    """Function returning stacked target from a data_loader.

    Adapted from https://github.com/adambielski/siamese-triplet
    """
    targets = []
    with torch.no_grad():
        for data, target in data_loader:
            target = target if len(target) > 0 else None
            if cuda and target is not None:
                target = target.cuda()

            targets.append(target)
    return torch.cat(targets, dim=0)

##My code for testing
def make_prediction(inner_net, inputs, prediction_length=9, num_simulations=7):

    net = ConditionalGenerativeModel(inner_net, size_auxiliary_variable=1, seed=0 + 1,
                                            number_generations_per_forward_call=num_simulations,
                                            base_measure='normal')

    inputs = torch.unsqueeze(inputs, 0)
    #print(inputs.shape)
    initialwindow = inputs  #  (1,10,1)
    outputs1 = net(inputs)   # 1, 7, 1
    #print(outputs1.shape)
    ensemble_length = outputs1.shape[1]

    # Cloning making list for grad 7 list of 1,10,1
    windowensemble = [initialwindow.clone() for _ in range(ensemble_length)]

    forecasts = []

    for step in range(prediction_length):
        onesteps = []

        for e in range(ensemble_length):
            eoutput = net(windowensemble[e])[:,e,:] #pick model e out

            #move ensemble window down, replace with model output
            #pop off output
            eoutput = eoutput.unsqueeze(-1)  #add dimension to line up without broadcasting
            shifted = torch.cat([windowensemble[e][:, 1:, :], eoutput], dim=1)  # 1,9,1 + 1,1,1    1,10,1
            windowensemble[e] = shifted

            onesteps.append(eoutput) 

        onesteps = torch.cat(onesteps, dim=0)

        if step == 0:
            forecasts = onesteps 
        else:
            forecasts = torch.cat((forecasts, onesteps), dim=1) 

    
    #print(forecasts.shape)

    # for i in range(prediction_length):
    #     outputs = net(inputs_new)
    #     outputs_new = outputs[:, 0, :]
    #     predictions[:, i, :] = outputs_new

    #     if i < prediction_length - 1:
    #         outputs_new = outputs_new.unsqueeze(1)          # => [batch, 1, data_size]
    #         inputs_new = torch.cat([inputs_new[:, 1:, :],   # => [batch, seq_len-1, data_size]
    #                             outputs_new],            # => [batch, 1, data_size]
    #                             dim=1)      
    return forecasts

def test_epochlongerpredictionbatch(val_loader, model, loss_fn, cuda, prediction_length,scaling_mean, scaling_std):
    """Function implementing the computation of the validation error, in batches.

    Adapted from https://github.com/adambielski/siamese-triplet
    """
    with torch.no_grad():
        model.eval()
        val_loss = 0

        for batch_idx, (data, target) in enumerate(val_loader):

            #data is 1,10,1 which is batchsize, window size, 1 
            #target is 1,9,1 which is batchsize, prediction_length, 1
            # if batch_idx ==1:
            #     break

            batchsize = data.shape[0]

            target = target if len(target) > 0 else None
            if not type(data) in (tuple, list):
                data = (data,)
            if cuda:
                data = tuple(d.cuda() for d in data)
                if target is not None:
                    target = target.cuda()

            outputs1 = model(*data)
            ensemble_length = outputs1.shape[1]
 
            batch_loss = torch.zeros(1,device = "cuda" if cuda else "cpu")

            for batch in range(batchsize):
                databatch = torch.unsqueeze(data[0][batch,:,:],0)
                targetbatch = torch.unsqueeze(target[batch,:,:],0)

                initialwindow = databatch  #  (1,10,1)

                # Cloning making list for grad 7 list of 1,10,1
                windowensemble = [initialwindow.clone() for _ in range(ensemble_length)]

                forecasts = []

                #model.resetseed()

                for step in range(prediction_length):
                    onesteps = []

                    for e in range(ensemble_length):
                        eoutput = model(windowensemble[e])[:,e,:] #pick model e out

                        #move ensemble window down, replace with model output
                        #pop off output
                        eoutput = eoutput.unsqueeze(1)  #add dimension to line up without broadcasting
                        shifted = torch.cat([windowensemble[e][:, 1:, :], eoutput], dim=1)  # 1,9,1 + 1,1,1    1,10,1
                        windowensemble[e] = shifted

                        onesteps.append(eoutput) 

                    onesteps = torch.cat(onesteps, dim=0)

                    if step == 0:
                        forecasts = onesteps 
                    else:
                        forecasts = torch.cat((forecasts, onesteps), dim=1) 

                loss = loss_fn(forecasts, targetbatch,scaling_mean, scaling_std)
                batch_loss += loss/batchsize
                val_loss += (loss/batchsize).item()

    return val_loss / (batch_idx + 1)  # divide here by the number of elements in the batch.

def train_epoch_longerpredictionbatch_single(train_loader, model, loss_fn, optimizer, cuda, prediction_length,scaling_mean, scaling_std):
    """Function implementing the training in one epoch.

    Adapted from https://github.com/adambielski/siamese-triplet
    """
    model.train()
    total_loss = 0

    for batch_idx, (data, target) in enumerate(train_loader):

        # if batch_idx >0:
        #     break
        # print(batch_idx)

        # print("Data (input):")
        # print(data)
        # print("Target (ground truth):")
        # print(target)
        #Try just calling only for batch_idx or change dataset
        
        #data is 1,10,1 which is batchsize, window size, 1 
        #target is 1,9,1 which is batchsize, prediction_length, 1

        batchsize = data.shape[0]

        target = target if len(target) > 0 else None
        if not type(data) in (tuple, list):
            data = (data,)
        if cuda:
            data = tuple(d.cuda() for d in data)
            if target is not None:
                target = target.cuda()

        optimizer.zero_grad()


        outputs1 = model(*data)
        ensemble_length = outputs1.shape[1]
        #print(ensemble_length)

        #print(data)
        #print(data[0].shape)
        batch_loss = torch.zeros(1,device = "cuda" if cuda else "cpu")

        for batch in range(batchsize):
            databatch = torch.unsqueeze(data[0][batch,:,:],0)
            targetbatch = torch.unsqueeze(target[batch,:,:],0)

            initialwindow = databatch  #  (1,10,1)

            # Cloning making list for grad 7 list of 1,10,1
            windowensemble = [initialwindow.clone() for _ in range(ensemble_length)]

            forecasts = []
        
            #Reset seed here 
            #model.resetseed()
            #seed changing for prediction length 
            # after initilaisation above make small function which gets called here to reset back to zero

            #print("batch loop")
            for step in range(prediction_length):
                onesteps = []

                #print("step loop:" + str(step))
                for e in range(ensemble_length):
                    #print("e loop:" + str(e))
                    eoutput = model(windowensemble[e])[:,e,:] #pick model e out

                    #move ensemble window down, replace with model output
                    #pop off output
                    eoutput = eoutput.unsqueeze(-1)  #add dimension to line up without broadcasting
                    shifted = torch.cat([windowensemble[e][:, 1:, :], eoutput], dim=1)  # 1,9,1 + 1,1,1    1,10,1
                    windowensemble[e] = shifted

                    onesteps.append(eoutput) 

                onesteps = torch.cat(onesteps, dim=0)

                if step == 0:
                    forecasts = onesteps 
                else:
                    forecasts = torch.cat((forecasts, onesteps), dim=1) 

            loss = loss_fn(forecasts, targetbatch,scaling_mean, scaling_std)
            batch_loss += loss/batchsize

            # if (loss < -1000):
            #     print(targetbatch)
            #     print(forecasts)
            #     print(scaling_mean)
            #     print(scaling_std)
        

        batch_loss.backward()
        optimizer.step()

        total_loss += batch_loss.item()

    return total_loss

def train_epoch_longerpredictionbatch(train_loader, model, loss_fn, optimizer, cuda, prediction_length,scaling_mean, scaling_std):
    """Function implementing the training in one epoch.

    Adapted from https://github.com/adambielski/siamese-triplet
    """
    model.train()
    total_loss = 0

    for batch_idx, (data, target) in enumerate(train_loader):

        #Try just calling only for batch_idx or change dataset
        
        #data is 1,10,1 which is batchsize, window size, 1 
        #target is 1,9,1 which is batchsize, prediction_length, 1
        # print(batch_idx)

        if batch_idx ==0:
            print('yo')
            print(data.shape)
            print(target.shape)
            #break


        # print(data[0,:,0,0,0])
        # print(target[0,:,0,0,0])
        # print(data[1,:,0,0,0])
        # print(target[1,:,0,0,0])
        # print(data[2,:,0,0,0])
        # print(target[2,:,0,0,0])


        batchsize = data.shape[0]

        target = target if len(target) > 0 else None
        if not type(data) in (tuple, list):
            data = (data,)
        if cuda:
            data = tuple(d.cuda() for d in data)
            if target is not None:
                target = target.cuda()

        optimizer.zero_grad()


        outputs1 = model(*data)

        #print(outputs1.shape)
        #print(brick)

        ensemble_length = outputs1.shape[1]
        #print(ensemble_length)

        #print(data)
        #print(data[0].shape)
        batch_loss = torch.zeros(1,device = "cuda" if cuda else "cpu")

        for batch in range(batchsize):
            #print('In batch loop')
            databatch = torch.unsqueeze(data[0][batch,:,:],0)
            targetbatch = torch.unsqueeze(target[batch,:,:],0)
            # print(databatch.shape)
            # print(targetbatch.shape)


            #print(databatch.shape)
            #print(targetbatch.shape)

            initialwindow = databatch  #  (1,10,1)

            # Cloning making list for grad 7 list of 1,10,1
            windowensemble = [initialwindow.clone() for _ in range(ensemble_length)]
            #print(windowensemble.shape)

            forecasts = []
        
            #Reset seed here 
            #model.resetseed()
            #seed changing for prediction length 
            # after initilaisation above make small function which gets called here to reset back to zero

            #print("batch loop")
            for step in range(prediction_length):
                onesteps = []

                #print("step loop:" + str(step))
                for e in range(ensemble_length):
                    #print("e loop:" + str(e))
                    eoutput = model(windowensemble[e])[:,e,:] #pick model e out
                    #print(eoutput.shape)

                    #move ensemble window down, replace with model output
                    #pop off output
                    eoutput = eoutput.unsqueeze(1)  #add dimension to line up without broadcasting
                    #print(windowensemble[e][:, 1:, :].shape)
                    #print(eoutput.shape)

                    shifted = torch.cat([windowensemble[e][:, 1:, :], eoutput], dim=1)  # 1,9,1 + 1,1,1    1,10,1
                    windowensemble[e] = shifted

                    onesteps.append(eoutput) 

                onesteps = torch.cat(onesteps, dim=0)

                if step == 0:
                    forecasts = onesteps 
                else:
                    forecasts = torch.cat((forecasts, onesteps), dim=1)


            print(forecasts.shape)
            print(targetbatch.shape)
            #print(brick) 

            loss = loss_fn(forecasts, targetbatch,scaling_mean, scaling_std)
            batch_loss += loss/batchsize
        
        #print(loss)
        batch_loss.backward()
        optimizer.step()

        total_loss += batch_loss.item()

    return total_loss / (batch_idx + 1)


def train_epoch_longerprediction(train_loader, model, loss_fn, optimizer, cuda, prediction_length,scaling_mean, scaling_std):
    """Function implementing the training in one epoch.

    Adapted from https://github.com/adambielski/siamese-triplet
    """
    model.train()
    total_loss = 0

    # inner_net = createGenerativeGRUNN(data_size=1, gru_hidden_size=8,
    #                                     noise_size=1,
    #                                     output_size=1, hidden_sizes=None, gru_layers=1,
    #                                     nonlinearity={"relu": torch.nn.functional.relu, "tanhshrink": torch.nn.functional.tanhshrink,
    #                    "leaky_relu": torch.nn.functional.leaky_relu}['leaky_relu'])()
    # input = torch.tensor([[-11.7746],
    #     [ -4.5585],
    #     [-12.4554],
    #     [ -2.7262],
    #     [ -9.7683],
    #     [ -2.0558],
    #     [ -0.8452],
    #     [-10.2474],
    #     [  7.8788],
    #     [  9.9928]])
    # next = torch.tensor([[ 9.9928],
    #     [ 6.3931],
    #     [ 9.6724],
    #     [ 6.0745],
    #     [ 8.3885],
    #     [ 6.9838],
    #     [ 5.1288],
    #     [14.3421],
    #     [-0.1680],
    #     [ 2.1470]])
    # test_predictions = make_prediction(inner_net, input, prediction_length) #from the starting window
    # print('Initial Window:', input)
    # print('Next 10 (9 prediction_length) steps', next)
    # print('predicted output from the model', test_predictions, test_predictions.shape)


    for batch_idx, (data, target) in enumerate(train_loader):

        #data is 1,10,1 which is batchsize, window size, 1 
        #target is 1,9,1 which is batchsize, prediction_length, 1
        #print('train_epoch_longerprediction')
        #print('batch_idx:' + str(batch_idx))
        #print("data")
        #print(data)
        #print("target")
        #print(target)
        target = target if len(target) > 0 else None
        if not type(data) in (tuple, list):
            data = (data,)
        if cuda:
            data = tuple(d.cuda() for d in data)
            if target is not None:
                target = target.cuda()

        optimizer.zero_grad()

        # print('batch_idx:',batch_idx, 'data0', data[0].shape)

        #data is 1,10,1 which is batchsize, window size, 1 
        
        #target is 1,9,1
        #Need to make input 7,9,1 for ensemble number
        # _, windowsize,_ = data.shape
        # # prediction_length = 9
        # #ensemble_length = 7, = outputs.shape[1]
        # finalinput = torch.zeros([7,9, 1])

        #Need to know ensemble length here?
        # print(len(data))
        # print(data[0])
        # print(data[0].shape[1])

        #Grabbing ensemble_length, could have function pass it, or just run once
        outputs1 = model(*data)
        ensemble_length = outputs1.shape[1]
        # print(ensemble_length)
        # print("^ensemble_length?")
        #print(outputs1.shape)

        #data = data.double()
        #target = target.double()

        #This side must have float 32, but my scoring rule must have double...
    

        # #In this case batch is 1, nsemble_length is 7, windowsize = 10, dim =1, prediction length =9
        # #windowsize = data[0].shape[1]
        # windowensemble = data[0].repeat(ensemble_length,1,1) #7,10,1 window array 

        # forecasts = []

        # #Very inefficient 1/ensemble_length efficiency 
        # for i in range(prediction_length):
        #     onesteps = []
        #     for e in range(ensemble_length):
        #         ensemblewindow = windowensemble[e].unsqueeze(0) #10,1 to 1,10,1 
        #         eoutput = model(ensemblewindow)[:,e,:] #pick model e out

        #         onesteps.append(eoutput)
        #         # print('eouput' + str(eoutput.shape))

        #         #move ensemble window down, replace with model output
        #         temp = windowensemble[e, 1:, :].clone()
        #         windowensemble[e, :-1, :] = temp
        #         windowensemble[e, -1, :]  = eoutput  # shape [1,1] broadcast into [1]? Need to test - seems fine
            
        #     onesteps = torch.stack(onesteps,dim=0) #7,1,1 now we have all ensemble steps 

        #     if i == 0:
        #         forecasts = onesteps
        #     else:
        #         forecasts = torch.cat((forecasts,onesteps),dim=1)
            
        #     # print("forecasts.shape")
        #     # print(forecasts.shape)

        # #print 
        # print("testing if compatible")
        # print(forecasts.shape)
        # print(target.shape)
        
        # loss_inputs = [forecasts,target]
        # loss_outputs = loss_fn(*loss_inputs)
        # loss = loss_outputs[0] if type(loss_outputs) in (tuple, list) else loss_outputs
        # total_loss += loss.item()

        ## Fix becuase of slicing causing gradient issues 

        initialwindow = data[0]  #  (1,10,1)
        outputs1 = model(*data)   # 1, 7, 1
        ensemble_length = outputs1.shape[1]

        # Cloning making list for grad 7 list of 1,10,1
        windowensemble = [initialwindow.clone() for _ in range(ensemble_length)]

        forecasts = []
        for step in range(prediction_length):
            onesteps = []

            for e in range(ensemble_length):
                eoutput = model(windowensemble[e])[:,e,:] #pick model e out

                #move ensemble window down, replace with model output
                #pop off output
                eoutput = eoutput.unsqueeze(-1)  #add dimension to line up without broadcasting
                shifted = torch.cat([windowensemble[e][:, 1:, :], eoutput], dim=1)  # 1,9,1 + 1,1,1    1,10,1
                windowensemble[e] = shifted

                onesteps.append(eoutput) 

            onesteps = torch.cat(onesteps, dim=0)

            if step == 0:
                forecasts = onesteps 
            else:
                forecasts = torch.cat((forecasts, onesteps), dim=1) 

        loss = loss_fn(forecasts, target,scaling_mean, scaling_std)
        total_loss += loss.item()

        loss.backward()
        optimizer.step()

    return total_loss / (batch_idx + 1)

        #Now I have tensor of ensemble,prediction_length,1
        #Against targer observatins of 1,prediction_length,1 

        #1 batch for everything
            

        # data_new = data
        # for i in range(prediction_length):
        #     outputs = model(*data_new)
        #     #print('in loop')
        #     #print('outputs')
        #     #print(outputs)
        #     #1, 7,1 batch_length, ensemble_length, 1 
        #     #Predicted 1 step ahead for 5 batches, 7 ensemble model

        #     outputs_new = outputs[:, 0, :] # selecting the first simulation which will be used to predict the next one
        #     #Get seven outputs for ensemble, how do we add to the observations
        #     #print('outputsnew')
        #     #print(outputs_new)
        #     #
            
        #     #print('model outputs:',outputs.shape, outputs)

        #     # assert not torch.all(outputs[0][:, 0] == outputs[0][:, 1])

        #     if type(outputs) not in (tuple, list):
        #         print('trigger 1')
        #         outputs = (outputs,)

        #     loss_inputs = outputs
        #     #print('lossinputs')
        #     #print(loss_inputs)

        #     if target is not None:
        #         print('trigger 2')
        #         target_new = (target[:, i, :],)
        #         loss_inputs += target_new
            
        #     #print('lossinputs after trigger')
        #     #print(loss_inputs)
            
        #     loss_outputs = loss_fn(*loss_inputs)
        #     loss = loss_outputs[0] if type(loss_outputs) in (tuple, list) else loss_outputs
        #     total_loss += loss.item()

        #     if i < prediction_length - 1:
        #         data_new_0 = data_new[0]
        #         data_new_0 = data_new_0[:, 1:, :] # removing the first observation in the input
        #         data_new_0 = torch.cat((data_new_0, outputs_new.unsqueeze(1)), dim=1) # adding the new prediction
        #         data_new = (data_new_0,) 
        #         # print('data_new:',data_new[0]) 

    #     loss.backward()
    #     optimizer.step()

    # return total_loss / (batch_idx + 1)  # divide here by the number of elements in the batch.



