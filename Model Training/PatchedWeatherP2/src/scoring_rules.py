import logging
from abc import ABCMeta, abstractmethod
from typing import Sequence, Union

import sigkernel
import numpy as np
import torch
import torch.nn.functional as F
from torchtyping import TensorType, patch_typeguard

patch_typeguard()  # use before @typechecked


# define an abstract class for the SRs
class ScoringRule(metaclass=ABCMeta):
    """This is the abstract class for the ScoringRules"""

    @abstractmethod
    def estimate_score_batch(self, forecast: TensorType["batch", "ensemble_size", "data_size"],
                             verification: TensorType["batch", "data_size"]) -> TensorType[float]:
        """
        Add docstring
        """

        raise NotImplementedError


class MSEScore(ScoringRule):
    """ Estimates the SignatureKernel. Here, I assume the observations and simulations are lists of
    length respectively n_obs and n_sim. Then, for each fixed observation the n_sim simulations are used to estimate the
    scoring rule. Subsequently, the values are summed over each of the n_obs observations.

    Note this scoring rule is connected to the energy distance between probability distributions.
    """

    def __init__(self):
        self.logger = logging.getLogger("Signature Kernel Score")

    def estimate_score_batch(self, forecast: TensorType["batch", "ensemble_size", "data_size"],
                             verification: TensorType["batch", "data_size"]) -> TensorType[float]:
        """The previous implementation considered a set of simulations and a set of observations, and estimated the
        score separately for each observation with the provided simulations. Here instead we have a batch
        of (simulations, observation); then it corresponds to the one above when batch_size=1 and the observation size
        is =1. We want therefore an implementation which works parallely over batches."""

        #1,9,1 to 5,9,1
        observation_broadcasted = verification.expand_as(forecast)

        squared_differences = (forecast - observation_broadcasted) ** 2
        score = squared_differences.mean()

        return score


class SignatureKernelRBF(ScoringRule):
    """ Estimates the SignatureKernel. Here, I assume the observations and simulations are lists of
    length respectively n_obs and n_sim. Then, for each fixed observation the n_sim simulations are used to estimate the
    scoring rule. Subsequently, the values are summed over each of the n_obs observations.

    Note this scoring rule is connected to the energy distance between probability distributions.
    """

    def __init__(self):
        self.logger = logging.getLogger("Signature Kernel Score")

    def estimate_score_batch(self, forecast: TensorType["batch", "ensemble_size", "data_size"],
                             verification: TensorType["batch", "data_size"], mean:TensorType["meanval"], std:TensorType["stdval"]) -> TensorType[float]:
        """The previous implementation considered a set of simulations and a set of observations, and estimated the
        score separately for each observation with the provided simulations. Here instead we have a batch
        of (simulations, observation); then it corresponds to the one above when batch_size=1 and the observation size
        is =1. We want therefore an implementation which works parallely over batches."""

        #print(forecast.shape) 5,9,1
        #print(verification.shape) 1,9,1
        # ensemble size is the length size
        # verification is observations 

        #print('in scoring rule kernel sig')
        #print(forecast)
        #print(verification)
        #print(forecast[0,:,0])

        verification = (verification- mean) / std
        forecast = (forecast- mean) / std

        #print(forecast[0,:,0])

        static_kernel = sigkernel.RBFKernel(sigma = 9)  ## 9 or sqrt(9)
        dyadic_order = 2
        signature_kernel = sigkernel.SigKernel(static_kernel, dyadic_order)

        #Sig kernels transform function requires sending to numpy which can't work.
        batch_size, seq_len, feature_dim = forecast.shape

        # 1) Create a time vector, shape [seq_len]
        time_vec = torch.linspace(0, 1, steps=seq_len, device=forecast.device)

        # 2) Reshape to match x's batch and sequence dimensions
        #    We want [batch_size, seq_len, 1], so we expand along batch dimension
        time_vec = time_vec.unsqueeze(0).unsqueeze(-1)  # now [1, seq_len, 1]
        
        y = torch.cat([verification, time_vec], dim=-1)
        time_vec = time_vec.expand(batch_size, seq_len, 1)  # now [batch_size, seq_len, 1]
        X = torch.cat([forecast, time_vec], dim=-1)

        #print(y)
        #print(X)

        X = X.double()
        y = y.double()

        score = signature_kernel.compute_scoring_rule(X,y)
        #print(score)
        #requires double, not float?

        return(score)

class SignatureKernel(ScoringRule):
    """ Estimates the SignatureKernel. Here, I assume the observations and simulations are lists of
    length respectively n_obs and n_sim. Then, for each fixed observation the n_sim simulations are used to estimate the
    scoring rule. Subsequently, the values are summed over each of the n_obs observations.

    Note this scoring rule is connected to the energy distance between probability distributions.
    """

    def __init__(self):
        self.logger = logging.getLogger("Signature Kernel Score")
        self.weights = np.array([0.07704437, 0.23039114, 0.38151911, 0.52897285, 0.67133229,
       0.80722643, 0.93534654, 1.05445875, 1.16341595, 1.26116882,
       1.34677594, 1.41941287, 1.47838008, 1.52310968, 1.55317091,
       1.56827425, 1.56827425, 1.55317091, 1.52310968, 1.47838008,
       1.41941287, 1.34677594, 1.26116882, 1.16341595, 1.05445875,
       0.93534654, 0.80722643, 0.67133229, 0.52897285, 0.38151911,
       0.23039114, 0.07704437])

    def estimate_score_batch(self, forecast: TensorType["batch", "ensemble_size", "data_size"],
                             verification: TensorType["batch", "data_size"], mean:TensorType["meanval"], std:TensorType["stdval"]) -> TensorType[float]:
        """The previous implementation considered a set of simulations and a set of observations, and estimated the
        score separately for each observation with the provided simulations. Here instead we have a batch
        of (simulations, observation); then it corresponds to the one above when batch_size=1 and the observation size
        is =1. We want therefore an implementation which works parallely over batches."""
        #For weather Bench:
        #Input Forecast (Ensemble, PathLength, Lat, Long, 1)
        #Input Verification (1,PathLength,Lat,Long,1)


        # print(forecast.shape) #5,9,1
        # print(verification.shape) #1,9,1
        # # ensemble size is the length size
        # verification is observations 

        #print('in scoring rule kernel sig')
        #print(forecast)
        #print(verification)
        #print(forecast[0,:,0])

        verification = (verification- mean) / std
        forecast = (forecast- mean) / std
   
        static_kernel = sigkernel.RBFKernel(sigma=1) 
        dyadic_order = 1
        signature_kernel = sigkernel.SigKernel(static_kernel, dyadic_order)

        order = forecast.shape[2]*forecast.shape[3]*forecast.shape[4]
        ordersquared = np.sqrt(order)
        verificationlat = verification.reshape(verification.shape[0],verification.shape[1],verification.shape[2]*verification.shape[3]*verification.shape[4])/ordersquared
        forecastlat = forecast.reshape(forecast.shape[0],forecast.shape[1],forecast.shape[2]*forecast.shape[3]*forecast.shape[4])/ordersquared
        #print(forecastlat.shape)
        #print(verificationlat.shape)

        #Sig kernels transform function requires sending to numpy which can't work.
        batch_size, seq_len, feature_dim = forecastlat.shape


        zerosX = torch.zeros(batch_size, 1, feature_dim, device=forecastlat.device)
        zerosY = torch.zeros(1, 1, feature_dim, device=forecastlat.device)
        #print(zerosX.shape)
        #print(zerosY.shape)
        #forecast  # Shape: [batch_size, 1, feature_dim]
        X = torch.cat([zerosX, forecastlat], dim=1)
        y = torch.cat([zerosY, verificationlat], dim=1)
        #print(X.shape)
        #print(y.shape)

                #1) Create a time vector, shape [seq_len]
        time_vec = torch.linspace(0, 1, steps=seq_len+1, device=forecastlat.device)
        # 2) Reshape to match x's batch and sequence dimensions
        #    We want [batch_size, seq_len, 1], so we expand along batch dimension
        time_vec = time_vec.unsqueeze(0).unsqueeze(-1)  # now [1, seq_len, 1]
    
        y = torch.cat([y, time_vec], dim=-1)
        time_vec = time_vec.expand(batch_size, seq_len+1, 1)  # now [batch_size, seq_len, 1]
        X = torch.cat([X, time_vec], dim=-1)

        X = X.double()
        y = y.double()

        #print(X.shape)
        #print(y.shape)

        score = signature_kernel.compute_scoring_rule(X,y)

        return(score)
    

class SignatureKernelPatches(ScoringRule):
    """ Estimates the SignatureKernel. Here, I assume the observations and simulations are lists of
    length respectively n_obs and n_sim. Then, for each fixed observation the n_sim simulations are used to estimate the
    scoring rule. Subsequently, the values are summed over each of the n_obs observations.

    Note this scoring rule is connected to the energy distance between probability distributions.
    """

    def __init__(self):
        self.logger = logging.getLogger("Signature Kernel Score")
        # Revised patch coordinates: 3 latitude bands × 8 longitude positions
        self.patch_coords = [
            (0, 0), (0, 8), (0, 16), (0, 24), (0, 32), (0, 40), (0, 48), (0, 56),
            (8, 0), (8, 8), (8, 16), (8, 24), (8, 32), (8, 40), (8, 48), (8, 56),
            (16, 0), (16, 8), (16, 16), (16, 24), (16, 32), (16, 40), (16, 48), (16, 56)
        ]
        self.patch_size = 16
    
    def get_patch(self,data, lat_start, lon_start):
        """Extract 16x16 patch from 32x64 grid with longitude wrapping"""
        # Latitude slice (straightforward)
        lat_slice = slice(lat_start, lat_start + self.patch_size)
        
        # Longitude handling (wrapping)
        if lon_start + self.patch_size <= 64:
            lon_slice = slice(lon_start, lon_start + self.patch_size)
            return data[..., lat_slice, lon_slice, :]
        else:
            # Split into two parts: end + beginning
            part1 = data[:,:, lat_slice, lon_start:, :]
            part2 = data[:,:, lat_slice, :lon_start + self.patch_size - 64, :]
            return torch.cat((part1, part2), dim=-2)

    def estimate_score_batch(self, forecast: TensorType["batch", "ensemble_size", "data_size"],
                             verification: TensorType["batch", "data_size"], mean:TensorType["meanval"], std:TensorType["stdval"]) -> TensorType[float]:
        """The previous implementation considered a set of simulations and a set of observations, and estimated the
        score separately for each observation with the provided simulations. Here instead we have a batch
        of (simulations, observation); then it corresponds to the one above when batch_size=1 and the observation size
        is =1. We want therefore an implementation which works parallely over batches."""
        #For weather Bench:
        #Input Forecast (Ensemble, PathLength, Lat, Long, 1)
        #Input Verification (1,PathLength,Lat,Long,1)

        verification = (verification- mean) / std
        forecast = (forecast- mean) / std

        static_kernel = sigkernel.RBFKernel(sigma=1) 
        dyadic_order = 1
        signature_kernel = sigkernel.SigKernel(static_kernel, dyadic_order)

        totalscore = []

        for lat_start, lon_start in self.patch_coords:
            forecast_patch = self.get_patch(forecast, lat_start, lon_start)
            verification_patch = self.get_patch(verification, lat_start, lon_start)

            order = forecast_patch.shape[2]*forecast_patch.shape[3]*forecast_patch.shape[4]
            ordersquared = np.sqrt(order)

            verificationlat = verification_patch.reshape(verification_patch.shape[0],verification_patch.shape[1],verification_patch.shape[2]*verification_patch.shape[3]*verification_patch.shape[4])/ordersquared
            forecastlat = forecast_patch.reshape(forecast_patch.shape[0],forecast_patch.shape[1],forecast_patch.shape[2]*forecast_patch.shape[3]*forecast_patch.shape[4])/ordersquared
        #print(forecastlat.shape)
        #print(verificationlat.shape)

        #Sig kernels transform function requires sending to numpy which can't work.
            batch_size, seq_len, feature_dim = forecastlat.shape


            zerosX = torch.zeros(batch_size, 1, feature_dim, device=forecastlat.device)
            zerosY = torch.zeros(1, 1, feature_dim, device=forecastlat.device)
            #print(zerosX.shape)
            #print(zerosY.shape)
            #forecast  # Shape: [batch_size, 1, feature_dim]
            X = torch.cat([zerosX, forecastlat], dim=1)
            y = torch.cat([zerosY, verificationlat], dim=1)
            #print(X.shape)
            #print(y.shape)

                    #1) Create a time vector, shape [seq_len]
            time_vec = torch.linspace(0, 1, steps=seq_len+1, device=forecastlat.device)
            # 2) Reshape to match x's batch and sequence dimensions
            #    We want [batch_size, seq_len, 1], so we expand along batch dimension
            time_vec = time_vec.unsqueeze(0).unsqueeze(-1)  # now [1, seq_len, 1]
        
            y = torch.cat([y, time_vec], dim=-1)
            time_vec = time_vec.expand(batch_size, seq_len+1, 1)  # now [batch_size, seq_len, 1]
            X = torch.cat([X, time_vec], dim=-1)

            X = X.double()
            y = y.double()

            #print(X.shape)
            #print(y.shape)

            score = signature_kernel.compute_scoring_rule(X,y)
            #print(score)
            #print(brick)
            totalscore.append(score)
        #print(score)
        #requires double, not float?
        totalscore = torch.stack(totalscore).sum()

        return(totalscore)

class SignatureEnergyPatchCombined(ScoringRule):
    "Combines SignatureKernel and EnergyScorePath"
        
    def __init__(self, beta=1, mean=True):
        """default value is beta=1"""
        self.beta = beta
        self.logger = logging.getLogger("Combined Score")
        self.mean = mean

        if not 0 < beta < 2:
            self.logger.warning("Beta should be in (0,2) for the Energy Score to be strictly proper. Computations "
                                "will still proceed but the results may be incongrous.")
            
        self.patch_coords = [
            (0, 0), (0, 8), (0, 16), (0, 24), (0, 32), (0, 40), (0, 48), (0, 56),
            (8, 0), (8, 8), (8, 16), (8, 24), (8, 32), (8, 40), (8, 48), (8, 56),
            (16, 0), (16, 8), (16, 16), (16, 24), (16, 32), (16, 40), (16, 48), (16, 56)
        ]
        self.patch_size = 16
    
    def get_patch(self,data, lat_start, lon_start):
        """Extract 16x16 patch from 32x64 grid with longitude wrapping"""
        # Latitude slice (straightforward)
        lat_slice = slice(lat_start, lat_start + self.patch_size)
        
        # Longitude handling (wrapping)
        if lon_start + self.patch_size <= 64:
            lon_slice = slice(lon_start, lon_start + self.patch_size)
            return data[..., lat_slice, lon_slice, :]
        else:
            # Split into two parts: end + beginning
            part1 = data[:,:, lat_slice, lon_start:, :]
            part2 = data[:,:, lat_slice, :lon_start + self.patch_size - 64, :]
            return torch.cat((part1, part2), dim=-2)

    def signaturecompute(self, forecast: TensorType["batch", "ensemble_size", "data_size"],
                             verification: TensorType["batch", "data_size"], mean:TensorType["meanval"], std:TensorType["stdval"]) -> TensorType[float]:
        """The previous implementation considered a set of simulations and a set of observations, and estimated the
        score separately for each observation with the provided simulations. Here instead we have a batch
        of (simulations, observation); then it corresponds to the one above when batch_size=1 and the observation size
        is =1. We want therefore an implementation which works parallely over batches."""
        #For weather Bench:
        #Input Forecast (Ensemble, PathLength, Lat, Long, 1)
        #Input Verification (1,PathLength,Lat,Long,1)

        verification = (verification- mean) / std
        forecast = (forecast- mean) / std

        static_kernel = sigkernel.RBFKernel(sigma=1) 
        dyadic_order = 1
        signature_kernel = sigkernel.SigKernel(static_kernel, dyadic_order)

        totalscore = []

        for lat_start, lon_start in self.patch_coords:
            forecast_patch = self.get_patch(forecast, lat_start, lon_start)
            verification_patch = self.get_patch(verification, lat_start, lon_start)

            order = forecast_patch.shape[2]*forecast_patch.shape[3]*forecast_patch.shape[4]
            ordersquared = np.sqrt(order)

            verificationlat = verification_patch.reshape(verification_patch.shape[0],verification_patch.shape[1],verification_patch.shape[2]*verification_patch.shape[3]*verification_patch.shape[4])/ordersquared
            forecastlat = forecast_patch.reshape(forecast_patch.shape[0],forecast_patch.shape[1],forecast_patch.shape[2]*forecast_patch.shape[3]*forecast_patch.shape[4])/ordersquared
        #print(forecastlat.shape)
        #print(verificationlat.shape)

        #Sig kernels transform function requires sending to numpy which can't work.
            batch_size, seq_len, feature_dim = forecastlat.shape


            zerosX = torch.zeros(batch_size, 1, feature_dim, device=forecastlat.device)
            zerosY = torch.zeros(1, 1, feature_dim, device=forecastlat.device)
            #print(zerosX.shape)
            #print(zerosY.shape)
            #forecast  # Shape: [batch_size, 1, feature_dim]
            X = torch.cat([zerosX, forecastlat], dim=1)
            y = torch.cat([zerosY, verificationlat], dim=1)
            #print(X.shape)
            #print(y.shape)

                    #1) Create a time vector, shape [seq_len]
            time_vec = torch.linspace(0, 1, steps=seq_len+1, device=forecastlat.device)
            # 2) Reshape to match x's batch and sequence dimensions
            #    We want [batch_size, seq_len, 1], so we expand along batch dimension
            time_vec = time_vec.unsqueeze(0).unsqueeze(-1)  # now [1, seq_len, 1]
        
            y = torch.cat([y, time_vec], dim=-1)
            time_vec = time_vec.expand(batch_size, seq_len+1, 1)  # now [batch_size, seq_len, 1]
            X = torch.cat([X, time_vec], dim=-1)

            X = X.double()
            y = y.double()

            #print(X.shape)
            #print(y.shape)

            score = signature_kernel.compute_scoring_rule(X,y)
            #print(score)
            #print(brick)
            totalscore.append(score)
        #print(score)
        #requires double, not float?
        totalscore = torch.stack(totalscore).sum()

        return(totalscore)
    
    def energycompute(self, forecast: TensorType["batch", "ensemble_size", "data_size"],
                             verification: TensorType["batch", "data_size"],scaling_mean,scaling_std) -> TensorType[float]:
        """The previous implementation considered a set of simulations and a set of observations, and estimated the
        score separately for each observation with the provided simulations. Here instead we have a batch
        of (simulations, observation); then it corresponds to the one above when batch_size=1 and the observation size
        is =1. We want therefore an implementation which works parallely over batches."""

        verification = verification.squeeze(-1)  # (1, 7, 32, 64)
        forecast = forecast.squeeze(-1)          # (3, 7, 32, 64)

        vals = []

        for lat in range(forecast.shape[2]):
            veriflat = verification[:, :, lat, :]  # (1, 7, 64)
            forelat = forecast[:, :, lat, :]       # (3, 7, 64)

            forelat = forelat.permute(1, 0, 2)     # (pathlength=7, ensemble=3, dims=64)
            veriflat = veriflat.permute(1, 0, 2)   # (pathlength=7, 1, dims=64)

            # diff_X_y: (7, 3)
            diff_X_y = torch.cdist(veriflat, forelat, p=2).squeeze(1)
            diff_X_tildeX = torch.cdist(forelat, forelat, p=2)  # (7, 3, 3)

            ensemble_size = forelat.shape[1]

            energy_scores = (
                2 * diff_X_y.mean(dim=1) -
                diff_X_tildeX.sum(dim=(1, 2)) / (ensemble_size * (ensemble_size - 1))
            )

            mean_score = energy_scores.mean()  # (scalar tensor)
            vals.append(mean_score)

        # Convert vals to tensor: (32,)
        energy_scores_lat = torch.stack(vals, dim=0).sum()

        return energy_scores_lat
    

    def estimate_score_batch(self, forecast: TensorType["batch", "ensemble_size", "data_size"],
                             verification: TensorType["batch", "data_size"], mean:TensorType["meanval"], std:TensorType["stdval"]) -> TensorType[float]:
    
        signaturescore = self.signaturecompute(forecast, verification, mean, std)
        energyscore = self.energycompute(forecast, verification, mean, std)

        print(f"signaturescore: {signaturescore}")
        print(f"energyscore: {energyscore}")

        energyscaled = energyscore / 64
        print(f"energyscore scaled: {energyscaled}")

        combined = (signaturescore + energyscaled)
        print(f"combined: {combined}")

        return combined



class EnergyScorePath(ScoringRule):
    """ Estimates the EnergyScore. Here, I assume the observations and simulations are lists of
    length respectively n_obs and n_sim. Then, for each fixed observation the n_sim simulations are used to estimate the
    scoring rule. Subsequently, the values are summed over each of the n_obs observations.

    Note this scoring rule is connected to the energy distance between probability distributions.
    """

    def __init__(self, beta=1, mean=True):
        """default value is beta=1"""
        self.beta = beta
        self.logger = logging.getLogger("Energy Score")

        self.mean = mean
        self.weights = np.array([0.07704437, 0.23039114, 0.38151911, 0.52897285, 0.67133229,
       0.80722643, 0.93534654, 1.05445875, 1.16341595, 1.26116882,
       1.34677594, 1.41941287, 1.47838008, 1.52310968, 1.55317091,
       1.56827425, 1.56827425, 1.55317091, 1.52310968, 1.47838008,
       1.41941287, 1.34677594, 1.26116882, 1.16341595, 1.05445875,
       0.93534654, 0.80722643, 0.67133229, 0.52897285, 0.38151911,
       0.23039114, 0.07704437])

        if not 0 < beta < 2:
            self.logger.warning("Beta should be in (0,2) for the Energy Score to be strictly proper. Computations "
                                "will still proceed but the results may be incongrous.")


    def estimate_score_batch(self, forecast: TensorType["batch", "ensemble_size", "data_size"],
                             verification: TensorType["batch", "data_size"],scaling_mean,scaling_std) -> TensorType[float]:
        """The previous implementation considered a set of simulations and a set of observations, and estimated the
        score separately for each observation with the provided simulations. Here instead we have a batch
        of (simulations, observation); then it corresponds to the one above when batch_size=1 and the observation size
        is =1. We want therefore an implementation which works parallely over batches."""

        verification = verification.squeeze(-1)  # (1, 7, 32, 64)
        forecast = forecast.squeeze(-1)          # (3, 7, 32, 64)

        verification = verification.reshape(verification.shape[0],verification.shape[1],verification.shape[2]*verification.shape[3])
        forecast = forecast.reshape(forecast.shape[0],forecast.shape[1],forecast.shape[2]*forecast.shape[3])

        forelat = forecast.permute(1, 0, 2)     # (pathlength=7, ensemble=3, dims=64)
        veriflat = verification.permute(1, 0, 2)   # (pathlength=7, 1, dims=64)

        # diff_X_y: (7, 3)
        diff_X_y = torch.cdist(veriflat, forelat, p=2).squeeze(1)
        diff_X_tildeX = torch.cdist(forelat, forelat, p=2)  # (7, 3, 3)

        ensemble_size = forelat.shape[1]

        energy_scores = (
            2 * diff_X_y.mean(dim=1) -
            diff_X_tildeX.sum(dim=(1, 2)) / (ensemble_size * (ensemble_size - 1))
        )

        mean_score = energy_scores.mean()  # (scalar tensor)

        return mean_score
        # vals = []
        # print(forecast.shape)
        # print(verification.shape)
        # forecast = forecast.squeeze(-1)         # [E, T, 32, 64]
        # verification = verification.squeeze(-1) # [1, T, 32, 64]print(forecast.shape)
        # forecast = forecast.permute(2, 0, 1, 3)         # [32, E, T, 64]
        # verification = verification.permute(2, 0, 1, 3) # [32, 1, T, 64]
        
        # print(forecast.shape)
        # print(verification.shape)
        # for i in range(forecast.shape[1]):
        #     #print(i)
        #     forcasttemp = forecast[:,i,:]
        #     verificationtemp = verification[:,i,:]
        #     forcasttemp = forcasttemp.unsqueeze(0)
        #     #print(forcasttemp.shape)
        #     #print(verificationtemp.shape)


        #     batch_size, ensemble_size, data_size = forcasttemp.shape

        #     diff_X_y = torch.cdist(verificationtemp.reshape(batch_size, 1, data_size), forcasttemp, p=2)
        #     diff_X_y = torch.squeeze(diff_X_y, dim=1)

        #     diff_X_tildeX = torch.cdist(forcasttemp, forcasttemp, p=2)

        #     if self.beta != 1:
        #         diff_X_tildeX = torch.pow(diff_X_tildeX, self.beta)
        #         diff_X_y = torch.pow(diff_X_y, self.beta)

        #     result = 2 * torch.sum(torch.mean(diff_X_y, dim=1)) - torch.sum(diff_X_tildeX) / (
        #             ensemble_size * (ensemble_size - 1))

        #     if self.mean:
        #         result /= verificationtemp.shape[0]
            
        #     vals.append(result)
        # torchvals = torch.stack(vals, dim=0).mean(dim=0)

        # return torchvals
    

class EnergyScore(ScoringRule):
    """ Estimates the EnergyScore. Here, I assume the observations and simulations are lists of
    length respectively n_obs and n_sim. Then, for each fixed observation the n_sim simulations are used to estimate the
    scoring rule. Subsequently, the values are summed over each of the n_obs observations.

    Note this scoring rule is connected to the energy distance between probability distributions.
    """

    def __init__(self, beta=1, mean=True):
        """default value is beta=1"""
        self.beta = beta
        self.logger = logging.getLogger("Energy Score")

        self.mean = mean

        if not 0 < beta < 2:
            self.logger.warning("Beta should be in (0,2) for the Energy Score to be strictly proper. Computations "
                                "will still proceed but the results may be incongrous.")

    def score(self, observations, simulations):
        """observations is an array of size (n_obs, p) (p being the dimensionality), while simulations is an array
        of size (n_sim, p).  This works on numpy in the framework of the genBayes with SR paper.
        We estimate this by building an empirical unbiased estimate of Eq. (2) in Ziel and Berk 2019"""

        return self.estimate_energy_score_numpy(observations, simulations)

    def estimate_energy_score_numpy(self, observations, simulations):
        """observations is an array of size (n_obs, p) (p being the dimensionality), while simulations is an array
        of size (n_sim, p). This works on numpy in the framework of the genBayes with SR paper.
        We estimate this by building an empirical unbiased estimate of Eq. (2) in Ziel and Berk 2019"""
        n_obs = observations.shape[0]
        n_sim, p = simulations.shape
        diff_X_y = observations.reshape(n_obs, 1, -1) - simulations.reshape(1, n_sim, p)
        # check (specifically in case n_sim==p):
        # diff_X_y2 = np.zeros((observations.shape[0], *simulations.shape))
        # for i in range(observations.shape[0]):
        #     for j in range(n_sim):
        #         diff_X_y2[i, j] = observations[i] - simulations[j]
        # assert np.allclose(diff_X_y2, diff_X_y)
        diff_X_y = np.einsum('ijk, ijk -> ij', diff_X_y, diff_X_y)

        diff_X_tildeX = simulations.reshape(1, n_sim, p) - simulations.reshape(n_sim, 1, p)
        # check (specifically in case n_sim==p):
        # diff_X_tildeX2 = np.zeros((n_sim, n_sim, p))
        # for i in range(n_sim):
        #     for j in range(n_sim):
        #         diff_X_tildeX2[i, j] = simulations[j] - simulations[i]
        # assert np.allclose(diff_X_tildeX2, diff_X_tildeX)
        diff_X_tildeX = np.einsum('ijk, ijk -> ij', diff_X_tildeX, diff_X_tildeX)

        if self.beta != 2:
            diff_X_y **= (self.beta / 2.0)
            diff_X_tildeX **= (self.beta / 2.0)

        result = 2 * np.sum(np.mean(diff_X_y, axis=1)) - n_obs * np.sum(diff_X_tildeX) / (n_sim * (n_sim - 1))

        if self.mean:
            result /= observations.shape[0]

        return result

    def estimate_score_batch(self, forecast: TensorType["batch", "ensemble_size", "data_size"],
                             verification: TensorType["batch", "data_size"]) -> TensorType[float]:
        """The previous implementation considered a set of simulations and a set of observations, and estimated the
        score separately for each observation with the provided simulations. Here instead we have a batch
        of (simulations, observation); then it corresponds to the one above when batch_size=1 and the observation size
        is =1. We want therefore an implementation which works parallely over batches."""

        batch_size, ensemble_size, data_size = forecast.shape

        # old version: the gradient computation when using this failed, when taking the power of diff_X_tildeX, due to
        # that matrix containing 0 entries; if self.beta_over_2 < 1, the gradient had a 0 term in the denominator, which
        # lead to nan values. The new version uses a torch command which computes the pairwise distances and does not
        # lead to nan gradients. It is also slightly faster.
        # diff_X_y = verification.reshape(batch_size, 1, data_size) - forecast
        # diff_X_y = torch.einsum('bep, bep -> be', diff_X_y, diff_X_y)
        #
        # diff_X_tildeX = forecast.reshape(batch_size, 1, ensemble_size, data_size) - (forecast.reshape(
        #     batch_size, ensemble_size, 1,
        #     data_size))  # idea could be adding an epsilon for numerical stability, but does not seem to work.
        # diff_X_tildeX = torch.einsum('befp, befp -> bef', diff_X_tildeX, diff_X_tildeX)
        #
        # if self.beta_over_2 != 1:
        #     diff_X_y = torch.pow(diff_X_y, self.beta_over_2)
        #     diff_X_tildeX = torch.pow(diff_X_tildeX, self.beta_over_2)

        # the following should have shape  ["batch", "ensemble_size", "data_size"], contains all differences of each
        # verification from its own forecasts
        diff_X_y = torch.cdist(verification.reshape(batch_size, 1, data_size), forecast, p=2)
        diff_X_y = torch.squeeze(diff_X_y, dim=1)

        # the following should have shape  ["batch", "ensemble_size", "ensemble_size", "data_size"], contains all
        # differences of each verification from each other verification for each batch element
        diff_X_tildeX = torch.cdist(forecast, forecast, p=2)

        if self.beta != 1:
            diff_X_tildeX = torch.pow(diff_X_tildeX, self.beta)
            diff_X_y = torch.pow(diff_X_y, self.beta)

        result = 2 * torch.sum(torch.mean(diff_X_y, dim=1)) - torch.sum(diff_X_tildeX) / (
                ensemble_size * (ensemble_size - 1))

        if self.mean:
            result /= verification.shape[0]

        return result


class KernelScore(ScoringRule):

    def __init__(self, kernel="gaussian", biased_estimator=False, torch=True, mean=True, **kernel_kwargs):
        """
        Parameters
        ----------
        kernel : str or callable, optional
            Can be a string denoting the kernel, or a function. If a string, only gaussian is implemented for now; in
            that case, you can also provide an additional keyword parameter 'sigma' which is used as the sigma in the
            kernel.
        """

        self.mean = mean

        self.kernel_vectorized = False
        if not isinstance(kernel, str) and not callable(kernel):
            raise RuntimeError("'kernel' must be either a string or a function of two variables returning a scalar.")
        if isinstance(kernel, str):
            if kernel == "gaussian":
                if torch:
                    self.kernel = self.def_gaussian_kernel_torch(**kernel_kwargs)
                else:
                    self.kernel = self.def_gaussian_kernel(**kernel_kwargs)
                self.kernel_vectorized = True  # the gaussian kernel is vectorized
            elif kernel == "rational_quadratic":
                if torch:
                    self.kernel = self.def_rational_quadratic_kernel_torch(**kernel_kwargs)
                else:
                    self.kernel = self.def_rational_quadratic_kernel(**kernel_kwargs)
                self.kernel_vectorized = True  # the rational_quadratic kernel is vectorized
            else:
                raise NotImplementedError("The required kernel is not implemented.")
        else:
            self.kernel = kernel  # if kernel is a callable already

        self.biased_estimator = biased_estimator

    def score(self, observations, simulations):
        """observations is an array of size (n_obs, p) (p being the dimensionality), while simulations is an array
        of size (n_sim, p). This works on numpy in the framework of the genBayes with SR paper.
        We estimate this by building an empirical unbiased estimate of Eq. (2) in Ziel and Berk 2019"""

        # compute the Gram matrix
        K_sim_sim, K_obs_sim = self.compute_Gram_matrix(observations, simulations)

        # Estimate MMD
        if self.biased_estimator:
            result = self.MMD_V_estimator(K_sim_sim, K_obs_sim)
        else:
            result = self.MMD_unbiased(K_sim_sim, K_obs_sim)

        if self.mean:
            result /= observations.shape[0]
        return result

    def estimate_score_batch(self, forecast: TensorType["batch", "ensemble_size", "data_size"],
                             verification: TensorType["batch", "data_size"]) -> TensorType[float]:
        """The previous implementation considered a set of simulations and a set of observations, and estimated the
        score separately for each observation with the provided simulations. Here instead we have a batch
        of (simulations, observation); then it corresponds to the one above when batch_size=1 and the observation size
        is =1. We want therefore an implementation which works parallely over batches."""

        # compute the Gram matrix
        K_sim_sim, K_obs_sim = self.compute_Gram_matrix_batch(forecast, verification)

        # Estimate MMD
        if self.biased_estimator:
            result = self.MMD_V_estimator_batch(K_sim_sim, K_obs_sim)
        else:
            result = self.MMD_unbiased_batch(K_sim_sim, K_obs_sim)

        if self.mean:
            result /= verification.shape[0]

        return result

    @staticmethod
    def def_gaussian_kernel(sigma=1):
        sigma_2 = 2 * sigma ** 2

        def Gaussian_kernel_vectorized(X, Y):
            """Here X and Y have shape (n_samples_x, n_features) and (n_samples_y, n_features);
            this directly computes the kernel for all pairwise components"""
            XY = X.reshape(X.shape[0], 1, -1) - Y.reshape(1, Y.shape[0], -1)  # pairwise differences
            return np.exp(- np.einsum('xyi,xyi->xy', XY, XY) / sigma_2)

        return Gaussian_kernel_vectorized

    @staticmethod
    def def_rational_quadratic_kernel(alpha=1):
        # this follows definition in Bińkowski, M., Sutherland, D. J., Arbel, M., & Gretton, A. (2018).
        # Demystifying MMD GANs. arXiv preprint arXiv:1801.01401.
        alpha_2 = 2 * alpha

        def rational_quadratic_vectorized(X, Y):
            """Here X and Y have shape (n_samples_x, n_features) and (n_samples_y, n_features);
            this directly computes the kernel for all pairwise components"""
            XY = X.reshape(X.shape[0], 1, -1) - Y.reshape(1, Y.shape[0], -1)  # pairwise differences
            return (1 + np.einsum('xyi,xyi->xy', XY, XY) / alpha_2) ** (-alpha)

        return rational_quadratic_vectorized

    def compute_Gram_matrix(self, observations, simulations):

        if self.kernel_vectorized:
            K_sim_sim = self.kernel(simulations, simulations)
            K_obs_sim = self.kernel(observations, simulations)
        else:
            n_obs = observations.shape[0]
            n_sim = simulations.shape[0]

            K_sim_sim = np.zeros((n_sim, n_sim))
            K_obs_sim = np.zeros((n_obs, n_sim))

            for i in range(n_sim):
                # we assume the function to be symmetric; this saves some steps:
                for j in range(i, n_sim):
                    K_sim_sim[j, i] = K_sim_sim[i, j] = self.kernel(simulations[i], simulations[j])

            for i in range(n_obs):
                for j in range(n_sim):
                    K_obs_sim[i, j] = self.kernel(observations[i], simulations[j])

        return K_sim_sim, K_obs_sim

    @staticmethod
    def MMD_unbiased(K_sim_sim, K_obs_sim):
        # Adapted from https://github.com/eugenium/MMD/blob/2fe67cbc7378f10f3b273cfd8d8bbd2135db5798/mmd.py
        # The estimate when distribution of x is not equal to y
        n_obs, n_sim = K_obs_sim.shape

        t_obs_sim = (2. / n_sim) * np.sum(K_obs_sim)
        t_sim_sim = (1. / (n_sim * (n_sim - 1))) * np.sum(K_sim_sim - np.diag(np.diagonal(K_sim_sim)))

        return n_obs * t_sim_sim - t_obs_sim

    @staticmethod
    def MMD_V_estimator(K_sim_sim, K_obs_sim):
        # The estimate when distribution of x is not equal to y
        n_obs, n_sim = K_obs_sim.shape

        t_obs_sim = (2. / n_sim) * np.sum(K_obs_sim)
        t_sim_sim = (1. / (n_sim * n_sim)) * np.sum(K_sim_sim)

        return n_obs * t_sim_sim - t_obs_sim

    @staticmethod
    def def_gaussian_kernel_torch(sigma=1):
        sigma_2 = 2 * sigma ** 2

        def Gaussian_kernel_vectorized(X: TensorType["batch_size", "x_size", "data_size"],
                                       Y: TensorType["batch_size", "y_size", "data_size"]) -> TensorType[
            "batch_size", "x_size", "y_size"]:
            """Here X and Y have shape (n_samples_x, n_features) and (n_samples_y, n_features);
            this directly computes the kernel for all pairwise components"""
            XY = torch.cdist(X, Y)
            return torch.exp(- torch.pow(XY, 2) / sigma_2)

        return Gaussian_kernel_vectorized

    @staticmethod
    def def_rational_quadratic_kernel_torch(alpha=1):
        alpha_2 = 2 * alpha

        def rational_quadratic_kernel_vectorized(X: TensorType["batch_size", "x_size", "data_size"],
                                                 Y: TensorType["batch_size", "y_size", "data_size"]) -> TensorType[
            "batch_size", "x_size", "y_size"]:
            """Here X and Y have shape (n_samples_x, n_features) and (n_samples_y, n_features);
            this directly computes the kernel for all pairwise components"""
            XY = torch.cdist(X, Y)
            return torch.pow(1 + torch.pow(XY, 2) / alpha_2, -alpha)

        return rational_quadratic_kernel_vectorized

    def compute_Gram_matrix_batch(self, forecast: TensorType["batch", "ensemble_size", "data_size"],
                                  verification: TensorType["batch", "data_size"]) -> (
            TensorType["batch", "ensemble_size", "ensemble_size"], TensorType["batch", 1, "ensemble_size"]):

        batch_size, ensemble_size, data_size = forecast.shape

        if self.kernel_vectorized:
            verification = verification.reshape(batch_size, 1, data_size)
            K_sim_sim = self.kernel(forecast, forecast)
            K_obs_sim = self.kernel(verification, forecast)
        else:

            K_sim_sim = torch.zeros((batch_size, ensemble_size, ensemble_size))
            K_obs_sim = torch.zeros((batch_size, 1, ensemble_size))

            for b in range(batch_size):
                for i in range(ensemble_size):
                    # we assume the function to be symmetric; this saves some steps:
                    for j in range(i, ensemble_size):
                        K_sim_sim[b, j, i] = K_sim_sim[b, i, j] = self.kernel(forecast[b, i], forecast[b, j])

                for j in range(ensemble_size):
                    K_obs_sim[b, 0, j] = self.kernel(verification[b], forecast[b, j])

        return K_sim_sim, K_obs_sim

    @staticmethod
    def MMD_unbiased_batch(K_sim_sim: TensorType["batch", "ensemble_size", "ensemble_size"],
                           K_obs_sim: TensorType["batch", 1, "ensemble_size"]) -> TensorType[float]:
        # Adapted from https://github.com/eugenium/MMD/blob/2fe67cbc7378f10f3b273cfd8d8bbd2135db5798/mmd.py
        # The estimate when distribution of x is not equal to y
        batch_size, ensemble_size, _ = K_sim_sim.shape

        t_obs_sim = (2. / ensemble_size) * torch.sum(K_obs_sim)

        # sum only the off-diagonal elements of K_sim_sim: first set them to 0:
        # this does not work inside automatic differentiation!
        # K_sim_sim[:, range(ensemble_size), range(ensemble_size)] = 0
        # t_sim_sim = (1. / (ensemble_size * (ensemble_size - 1))) * torch.sum(K_sim_sim)

        # alternatively, sum only the off-diagonal elements.
        off_diagonal_sum = torch.sum(
            K_sim_sim.masked_select(
                torch.stack([~torch.eye(ensemble_size, dtype=bool, device=K_sim_sim.device)] * batch_size)))
        t_sim_sim = (1. / (ensemble_size * (ensemble_size - 1))) * off_diagonal_sum

        return t_sim_sim - t_obs_sim

    @staticmethod
    def MMD_V_estimator_batch(K_sim_sim: TensorType["batch", "ensemble_size", "ensemble_size"],
                              K_obs_sim: TensorType["batch", 1, "ensemble_size"]) -> TensorType[float]:
        # Adapted from https://github.com/eugenium/MMD/blob/2fe67cbc7378f10f3b273cfd8d8bbd2135db5798/mmd.py
        # The estimate when distribution of x is not equal to y
        batch_size, ensemble_size, _ = K_sim_sim.shape

        t_obs_sim = (2. / ensemble_size) * torch.sum(K_obs_sim)

        t_sim_sim = (1. / (ensemble_size * ensemble_size)) * torch.sum(K_sim_sim)

        return t_sim_sim - t_obs_sim

    # todo speed up by reciclying previous computations of 2. / ensemble_size and similar?


class VariogramScore(ScoringRule):
    """ Estimates the EnergyScore. Here, I assume the observations and simulations are lists of
    length respectively n_obs and n_sim. Then, for each fixed observation the n_sim simulations are used to estimate the
    scoring rule. Subsequently, the values are summed over each of the n_obs observations.

    Note this scoring rule is connected to the energy distance between probability distributions.
    """

    def __init__(self, p: float = 1.0, variogram: TensorType["data_size", "data_size"] = None, mean=True,
                 max_batch_size=None):
        """default value is beta=1"""
        self.p = p
        self.logger = logging.getLogger("Variogram Score")

        self.mean = mean
        # the variogram needs to be a d x d matrix, with d is the data_size considered here
        # could also allow it to be a string which would do some things
        self.variogram = variogram
        self.max_batch_size = max_batch_size  # max batch size to use in the computation, to avoid memory overflow

        if p <= 0:
            raise RuntimeError("You should use p > 0 for the variogram score.")

    def estimate_score_batch(self, forecast: TensorType["batch", "ensemble_size", "data_size"],
                             verification: TensorType["batch", "data_size"]) -> TensorType[float]:
        """The previous implementation considered a set of simulations and a set of observations, and estimated the
        score separately for each observation with the provided simulations. Here instead we have a batch
        of (simulations, observation); then it corresponds to the one above when batch_size=1 and the observation size
        is =1. We want therefore an implementation which works parallely over batches."""

        batch_size, ensemble_size, data_size = forecast.shape

        if self.variogram is None:
            variogram = torch.ones(data_size, data_size, device=forecast.device)
        else:
            variogram = self.variogram
            if variogram.shape[0] != verification.shape[1]:
                raise RuntimeError("The data size needs to be the same as the variogram size.")

        # naive version:
        # score = 0
        # for b in range(batch_size):
        #     for i in range(data_size):
        #         for j in range(data_size):
        #             diff_obs = torch.pow(torch.abs(verification[b, i] - verification[b, j]), self.p)
        #             diff_for=0
        #             for e in range(ensemble_size):
        #                 diff_for += torch.pow(torch.abs(forecast[b, e, i] - forecast[b, e, j]), self.p)
        #             diff_for /= ensemble_size
        #
        #             score += variogram[i, j] * torch.pow(diff_obs - diff_for, 2)

        if self.max_batch_size is None:
            diff_obs = torch.pow(torch.abs(verification.unsqueeze(1) - verification.unsqueeze(2)), self.p)
            diff_for = torch.mean(torch.pow(torch.abs(forecast.unsqueeze(3) - forecast.unsqueeze(2)), self.p), dim=1)
            diff = torch.pow(diff_for - diff_obs, 2)

            result = torch.einsum("ij,bij-> ", variogram, diff)
        else:
            i = 0
            result = 0
            while self.max_batch_size * i < batch_size:
                start_index = self.max_batch_size * i
                end_index = start_index + self.max_batch_size

                diff_obs = torch.pow(torch.abs(
                    verification[start_index:end_index].unsqueeze(1) - verification[start_index:end_index].unsqueeze(
                        2)),
                    self.p)
                diff_for = torch.mean(torch.pow(
                    torch.abs(
                        forecast[start_index:end_index].unsqueeze(3) - forecast[start_index:end_index].unsqueeze(2)),
                    self.p), dim=1)
                diff = torch.pow(diff_for - diff_obs, 2)

                result += torch.einsum("ij,bij-> ", variogram, diff)
                i += 1

        if self.mean:
            result /= batch_size

        return result


class SumScoringRules(ScoringRule):

    def __init__(self, scoring_rule_list: Sequence[ScoringRule], weight_list: Sequence[float] = None):

        self.n_srs = len(scoring_rule_list)
        if self.n_srs == 0:
            raise RuntimeError("You need to provide at least a scoring rule.")

        if weight_list is None:
            weight_list = [1.0] * self.n_srs
        else:
            if self.n_srs != len(weight_list):
                raise RuntimeError("The length of the scoring rules and weight lists have to be the same")

        # check that the provided scoring rules are ScoringRules
        for sr in scoring_rule_list:
            if not isinstance(sr, ScoringRule):
                raise RuntimeError("The provided scoring rules have to be instances of ScoringRules classes")

        self.scoring_rule_list = scoring_rule_list
        self.weight_list = weight_list

    def estimate_score_batch(self, forecast: Union[TensorType["batch", "ensemble_size", "data_size"], TensorType[
        "batch_size", "ensemble_size", "height", "width", "fields"]],
                             verification: Union[TensorType["batch", "data_size"], TensorType[
                                 "batch_size", "height", "width", "fields"]]) -> TensorType[float]:
        """We estimate the score for all the scoring rules in self.scoring_rule_list,
        multiply with the corresponding weight in self.weight_list and sum them"""

        # for implementation
        # sr_tot = 0
        # for i in range(self.n_srs):
        #     sr_tot += self.scoring_rule_list[i].estimate_score_batch(forecast, verification) * self.weight_list[i]

        # map implementation: a bit faster
        def _compute_sr(sr, weight):
            return sr.estimate_score_batch(forecast, verification) * weight

        sr_tot_2 = sum(list(map(_compute_sr, self.scoring_rule_list, self.weight_list)))

        return sr_tot_2


class PatchedScoringRule(ScoringRule):
    # todo next iteration: allow to use different scoring rules for each patch. This can be useful for instance for
    #  the kernel SR, you can use different bandwidths for the different patches

    def __init__(self, scoring_rule: ScoringRule, masks: TensorType["n_patches", "data_size", bool]):
        """
        When you call the `estimate_score_batch` method, the provided scoring_rule is computed on all patches
        defined by the masks and then summed over.

        :param scoring_rule: an instance of ScoringRule class.
        :param masks: Torch tensor, in which the first dimension denotes the number of patches and the second dimension
         the size of the data. Each entry is True or False according to whether that data component is part of
         the corresponding patch.
        """
        self.scoring_rule = scoring_rule
        self.masks = masks

    def estimate_score_batch(self, forecast: TensorType["batch", "ensemble_size", "data_size"],
                             verification: TensorType["batch", "data_size"]) -> TensorType[float]:
        """
        """

        # for implementation
        sr_tot = 0
        for i in range(self.masks.shape[0]):
            sr_tot += self.scoring_rule.estimate_score_batch(forecast[:, :, self.masks[i]],
                                                             verification[:, self.masks[i]])

        # map implementation: a bit slower, even with many masks
        # def _compute_sr(mask):
        #     return self.scoring_rule.estimate_score_batch(forecast[:, :, mask], verification[:, mask])
        #
        # sr_tot_2 = sum(list(map(_compute_sr, self.masks)))

        return sr_tot


class ScoringRulesForWeatherBench(ScoringRule):
    def __init__(self, scoring_rule: ScoringRule):
        """
        When you call the `estimate_score_batch` method, this method flattens the weatherbench data and computes the
        scoring rule on the flattened data.

        :param scoring_rule: an instance of ScoringRule class.
        """
        self.scoring_rule = scoring_rule

    def estimate_score_batch(self, forecast: TensorType["batch_size", "ensemble_size", "height", "width", "fields"],
                             verification: TensorType["batch_size", "height", "width", "fields"]) -> TensorType[float]:
        """
        """
        forecast = forecast.flatten(2, -1)
        verification = verification.flatten(1, -1)

        return self.scoring_rule.estimate_score_batch(forecast, verification)


class ScoringRulesForWeatherBenchPatched(ScoringRule):
    # Contrarily to PatchedScoringRule, this does not rely on using masks, but rather on the unfold function in
    # Pytorch, exploiting the structure of WeatherBench data

    def __init__(self, scoring_rule: ScoringRule, patch_step: int = 8, patch_size: int = 16):
        """
        When you call the `estimate_score_batch` method, this method flattens the weatherbench data and computes the
        scoring rule on the flattened data.

        :param scoring_rule: an instance of ScoringRule class.
        """
        self.scoring_rule = scoring_rule
        self.patch_step = patch_step
        self.patch_size = patch_size

    def estimate_score_batch(self, forecast: TensorType["batch_size", "ensemble_size", "height", "width", "fields"],
                             verification: TensorType["batch_size", "height", "width", "fields"]) -> TensorType[float]:
        """
        """
        # you first need to add the periodic boundary conditions, using padding: 
        padding = (0, 0, 0, self.patch_step, 0, self.patch_step)
        forecast = F.pad(forecast, pad=padding, mode='circular')  # as we have a 5d tensor, it expects a 3d padding size

        padding = (0, self.patch_step, 0, self.patch_step)
        verification = verification.permute(0, 3, 1, 2)
        verification = F.pad(verification, pad=padding,
                             mode='circular')  # as we have a 4d tensor, it expects a 2d padding size
        verification = verification.permute(0, 2, 3, 1)

        # Tensor.unfold replaces the unfolded dimension with the number of windows, and adds a last dimension with the
        # content of each window
        # dimension, size, step
        forecast = forecast.unfold(2, self.patch_size, self.patch_step)
        verification = verification.unfold(1, self.patch_size, self.patch_step)

        forecast = forecast.unfold(3, self.patch_size, self.patch_step)
        verification = verification.unfold(2, self.patch_size, self.patch_step)
        #  forecast: batch x ensemble x num_windows_height x num_windows_width x fields x patch_size x patch_size
        #  verification: batch x num_windows_height x num_windows_width x fields x patch_size x patch_size

        # swap the ensemble size dimension with num_windows_height and num_windows_width:
        forecast = forecast.permute(0, 2, 3, 1, 4, 5, 6)

        # now make forecast: TensorType["batch", "ensemble_size", "data_size"],
        #          verification: TensorType["batch", "data_size"]
        # the original batch, num_windows_height and num_windows_width make the new batch size;
        # fields x patch_size x patch_size make the new data_size
        forecast = forecast.flatten(0, 2).flatten(2, -1)
        verification = verification.flatten(0, 2).flatten(1, -1)

        return self.scoring_rule.estimate_score_batch(forecast, verification)


class LossForWeatherBenchPatched:
    """Same as ScoreForWeatherBenchPatched, but with a loss function rather than a SR."""

    def __init__(self, loss, patch_step: int = 8, patch_size: int = 16):
        """
        When you call the `estimate_score_batch` method, this method flattens the weatherbench data and computes the
        scoring rule on the flattened data.

        :param loss: a loss function.
        """
        self.loss = loss
        self.patch_step = patch_step
        self.patch_size = patch_size

    def __call__(self, forecast: TensorType["batch_size", "height", "width", "fields"],
                 verification: TensorType["batch_size", "height", "width", "fields"]) -> TensorType[float]:
        """
        """
        forecast = self.transform_tensor(forecast)
        verification = self.transform_tensor(verification)

        return self.loss(forecast, verification)

    def transform_tensor(self, tensor):
        # you first need to add the periodic boundary conditions, using padding:
        padding = (0, self.patch_step, 0, self.patch_step)
        tensor = tensor.permute(0, 3, 1, 2)
        tensor = F.pad(tensor, pad=padding,
                       mode='circular')  # as we have a 4d tensor, it expects a 2d padding size
        tensor = tensor.permute(0, 2, 3, 1)

        # Tensor.unfold replaces the unfolded dimension with the number of windows, and adds a last dimension with the
        # content of each window
        # dimension, size, step
        tensor = tensor.unfold(1, self.patch_size, self.patch_step)

        tensor = tensor.unfold(2, self.patch_size, self.patch_step)
        #  tensor: batch x num_windows_height x num_windows_width x fields x patch_size x patch_size

        #          tensor: TensorType["batch", "data_size"]
        # the original batch, num_windows_height and num_windows_width make the new batch size;
        # fields x patch_size x patch_size make the new data_size
        return tensor.flatten(0, 2).flatten(1, -1)


def estimate_score_chunks(scoring_rule: ScoringRule, forecast: TensorType["batch", "ensemble_size", "length", "data_size"],
                          verification: TensorType["batch", "length", "data_size"],scaling_mean,scaling_std, chunk_size=100) -> TensorType[float]:
    batch_size = verification.shape[0]
    cum_score = 0
    i = 0
    while i * chunk_size < batch_size:
        cum_score += scoring_rule.estimate_score_batch(forecast[i * chunk_size:(i + 1) * chunk_size,:,:,:].squeeze(0),
                                                       verification[i * chunk_size:(i + 1) * chunk_size,:,:],scaling_mean,scaling_std)
        i += 1
    return cum_score / i


# def estimate_score_chunks(
#     scoring_rule, 
#     forecast: torch.Tensor, 
#     verification: torch.Tensor, 
#     chunk_size=100
# ) -> torch.Tensor:
#     """
#     Estimate the scoring rule in chunks to avoid memory overflow.

#     Args:
#         scoring_rule: A scoring rule object with an `estimate_score_batch` method.
#         forecast: Tensor of shape [batch, ensemble_size, prediction_length, data_size]
#         verification: Tensor of shape [batch, prediction_length, data_size]
#         chunk_size: Number of samples processed per chunk.

#     Returns:
#         A float tensor representing the average score.
#     """

#     batch_size = verification.shape[0]
#     cum_score = 0
#     i = 0

#     while i * chunk_size < batch_size:
#         # Extract chunk slices
#         forecast_chunk = forecast[i * chunk_size:(i + 1) * chunk_size]
#         verification_chunk = verification[i * chunk_size:(i + 1) * chunk_size]

#         # Compute score for the chunk
#         cum_score += scoring_rule.estimate_score_batch(forecast_chunk, verification_chunk)
#         i += 1

#     return cum_score / i if i > 0 else torch.tensor(0.0)
