import math
import time

import numpy as np
import pytorch_lightning as pl
import torch
import torch.optim as optim
from torch import Tensor, nn
from torch.distributions.kl import kl_divergence
from torch.utils.data import DataLoader, TensorDataset

from bicycle.utils.training import EarlyStopperTorch, lyapunov_direct


def init_weights(m):
    if isinstance(m, (nn.Conv2d, nn.Linear, nn.ConvTranspose2d)):
        torch.nn.init.xavier_normal_(m.weight, gain=0.1)
        if m.bias is not None:
            # m.bias.data.fill_(0.01)
            m.bias.data.zero_()


class Encoder(nn.Module):
    def __init__(self, x_dim: int, z_dim: int, n_cond: int, act_fn: object = nn.GELU, hid_dim: int = 500):
        super().__init__()
        self.z_dim = z_dim
        self.net = nn.Sequential(
            nn.Linear(x_dim + n_cond, hid_dim),
            act_fn(),
            nn.Dropout(p=0.05),
            nn.Linear(hid_dim, hid_dim),
            act_fn(),
            nn.Dropout(p=0.05),
            nn.Linear(hid_dim, 2 * z_dim),
        )

        self.net.apply(init_weights)

    def forward(self, x):
        x = self.net(x)
        mu = x[:, : self.z_dim]
        variance = torch.nn.Softplus()(x[:, self.z_dim :]) + 1e-6
        return mu, variance


class Omega_Iterative(pl.LightningModule):
    def __init__(
        self,
        alphas,
        betas,
        B,
        sigmas,
        lr = 1e-2,
        sigma_min = 1e-3,        
        device = "cpu",
        rank_w_cov_factor = 0):
        
        super().__init__()
        super().to(device)

        self.pos = nn.Softplus()
        
        self.n_regimes = alphas.shape[0]

        self.sigma_min = sigma_min
        
        print('setting device to:',device)
        
        self.alphas = alphas.detach().to(device)
        self.betas = betas.detach().to(device)
        self.B = B.detach().to(device)
        self.sigmas = sigmas.detach().to(device)
        self.n_genes = alphas.shape[-1]

        self.loss = list()

        if rank_w_cov_factor > 0 and rank_w_cov_factor <= self.n_genes:
            self.rank_w_cov_factor = rank_w_cov_factor
        else:
            self.rank_w_cov_factor = self.n_genes
        
        self.lr = lr
        
        self.sigma_min = sigma_min
        
        self.w_cov_diag = torch.nn.Parameter(
                        torch.exp(
                            0.1
                            * torch.rand(
                                (
                                    self.n_genes,
                                )
                            )
                        )
                    )
        self.w_cov_factor = torch.nn.Parameter(
            0.1 * torch.randn((self.n_genes, self.rank_w_cov_factor))
        )
        
    def configure_optimizers(self):
        return optim.Adam(self.parameters(), lr=self.lr)        
        
    def training_step(self, batch):
        
        loss_lyapunov = torch.sum(torch.square(self.lyapunov_lhs() - self.lyapunov_rhs())) / (
                self.n_genes**2
            )        

        #print("\rLyapunov loss: %.2f" % loss_lyapunov)
        self.loss.append(loss_lyapunov.item())
        
        return loss_lyapunov

    def lyapunov_lhs(self):
        mat = self.B.detach() @ (
            torch.diag_embed(self.pos(self.w_cov_diag) + self.sigma_min)
            + self.w_cov_factor @ self.w_cov_factor.transpose(0, 1)
        )
        return mat + mat.transpose(0, 1)

    def lyapunov_rhs(self):
        return self.sigmas.detach() @ self.sigmas.transpose(0, 1).detach()


class BICYCLE(pl.LightningModule):
    def __init__(
        self,
        lr,
        gt_interv,
        n_genes,
        n_samples,
        lyapunov_penalty=True,
        perfect_interventions=True,
        rank_w_cov_factor=1,
        optimizer="adam",
        optimizer_kwargs: dict = {},
        device="cuda",
        scale_l1=1.0,
        scale_spectral=1.0,
        scale_lyapunov=1.0,
        scale_kl=1.0,
        early_stopping: bool = True,
        early_stopping_min_delta: float = 0.5,
        early_stopping_patience: int = 100,
        early_stopping_p_mode: bool = True,
        x_distribution: str = None,
        x_distribution_kwargs: dict = None,
        init_tensors: dict = {},
        mask: Tensor = None,
        use_encoder: bool = False,
        gt_beta: Tensor = None,
        train_gene_ko: list = None,
        test_gene_ko: list = None,
        use_latents: bool = True,
        covariates: Tensor = None,
        n_factors: int = 0,
        intervention_type: str = "dCas9",
        sigma_min: float = 1e-3,
        T: float = 1.0,
        learn_T: bool = False,
        train_only_likelihood: bool = False,
        train_only_latents: bool = False,
        mask_genes: list = [],
    ):
        """
        Parameters
        ----------
        covariates: torch.Tensor
            Covariates to be used in the model. If None, no covariates are used.
            Must be of shape (cells, n_covariates).
        """
        super().__init__()

        # FIXME / TODO
        # should we decrease self.sigma_min?

        self.save_hyperparameters()

        self.is_fitted = False
        self.init_tensors = init_tensors
        self.lr = lr
        self.gt_interv = gt_interv
        self.n_genes = n_genes
        self.early_stopping = early_stopping
        self.lyapunov_penalty = lyapunov_penalty
        self.perfect_interventions = perfect_interventions
        self.rank_w_cov_factor = rank_w_cov_factor
        self.optimizer = optimizer
        self.optimizer_kwargs = optimizer_kwargs
        self._device = device
        self.contexts = torch.arange(gt_interv.shape[1])
        self.n_contexts = gt_interv.shape[1]
        self.n_samples = n_samples
        self.mask = mask
        self.use_encoder = use_encoder
        self.use_latents = use_latents
        self.n_factors = n_factors
        self.intervention_type = intervention_type
        self.sigma_min = sigma_min
        self.train_only_likelihood = train_only_likelihood
        self.train_only_latents = train_only_latents
        self.mask_genes = mask_genes

        self.nll_mask = torch.ones(self.n_genes, device=gt_interv.device, dtype=torch.bool)
        if len(self.mask_genes) > 0:
            for g in self.mask_genes:
                self.nll_mask[g] = False

        if self.use_latents:
            if self.use_encoder:
                self.n_conditions = torch.sum(gt_interv.sum(axis=1) > 0).item()
                self.encoder = Encoder(x_dim=self.n_genes, z_dim=self.n_genes, n_cond=self.n_conditions)

                self.gt_nonzeros = self.gt_interv[~torch.all(self.gt_interv == 0, axis=1)]
            else:
                # Cell and gene specific latent expression values
                self.z_loc = torch.nn.Parameter(torch.zeros((self.n_samples, n_genes)))
                self.z_scale = torch.nn.Parameter(torch.zeros((self.n_samples, n_genes)))

        self.scale_l1 = scale_l1
        self.scale_spectral = scale_spectral
        self.scale_lyapunov = scale_lyapunov
        self.scale_kl = scale_kl
        self._normalisation_computed = False

        if x_distribution is not None:
            if x_distribution not in ["Poisson", "Normal", "NormalNormal", "Multinomial"]:
                raise ValueError(
                    f"Unknown distribution {x_distribution}. "
                    "Only Poisson, Normal, NormalNormal, Multinomial is supported."
                )
        self.x_distribution = x_distribution
        self.x_distribution_kwargs = x_distribution_kwargs

        if early_stopping:
            self.earlystopper = EarlyStopperTorch(
                mode="min",
                patience=early_stopping_patience,
                min_delta=early_stopping_min_delta,
                percentage=early_stopping_p_mode,
            )

        self.validation_step_outputs = []

        self.pos = nn.Softplus()

        # Variables describing *UNPERTURBED* mechanisms
        if self.mask is None:
            if self.n_factors == 0:
                if "beta" in self.init_tensors:
                    self.beta = torch.nn.Parameter(self.init_tensors["beta"])
                else:
                    self.beta = torch.nn.Parameter(0.001 * torch.randn((n_genes, n_genes)))
            else:
                print("Initializing gene2factor and factor2gene matrices!")
                self.gene2factor = torch.nn.Parameter(0.00001 * torch.randn((n_genes, n_factors)))
                self.factor2gene = torch.nn.Parameter(0.00001 * torch.randn((n_factors, n_genes)))
        else:
            if self.n_factors == 0:
                with torch.no_grad():
                    self.n_entries = (self.mask > 0.5).sum()
                    # print(f"No. entries in mask for beta: {self.n_entries}")
                    self.beta_idx = torch.where(self.mask > 0.5)
                self.beta_val = torch.nn.Parameter(0.001 * torch.randn((self.n_entries,)))
            else:
                raise NotImplementedError("Combination of factorization and masking not implemented yet.")

        if not self.perfect_interventions:
            if self.mask is None:
                if self.n_factors == 0:
                    self.beta_p = torch.nn.Parameter(0.1 * torch.randn((n_genes, n_genes)))
                else:
                    self.gene2factor_p = torch.nn.Parameter(0.00001 * torch.randn((n_genes, n_factors)))
                    self.factor2gene_p = torch.nn.Parameter(0.00001 * torch.randn((n_factors, n_genes)))
            else:
                if self.n_factors == 0:
                    self.beta_p_val = torch.nn.Parameter(0.1 * torch.randn((self.n_entries)))
                else:
                    raise NotImplementedError("Combination of factorization and masking not implemented yet.")
        else:
            if self.mask is None:
                if self.n_factors == 0:
                    self.beta_p = torch.nn.Parameter(torch.zeros((n_genes, n_genes)))
                    self.beta_p.requires_grad = False
                else:
                    self.gene2factor_p = torch.nn.Parameter(torch.zeros((n_genes, n_factors)))
                    self.gene2factor_p.requires_grad = False
                    self.factor2gene_p = torch.nn.Parameter(torch.zeros((n_factors, n_genes)))
                    self.factor2gene_p.requires_grad = False
            else:
                if self.n_factors == 0:
                    self.beta_p_val = torch.nn.Parameter(torch.zeros((self.n_entries)))
                    self.beta_p_val.requires_grad = False
                else:
                    raise NotImplementedError("Combination of factorization and masking not implemented yet.")

        # Must be positive
        self.alpha = torch.nn.Parameter(0.001 * torch.exp(torch.randn((n_genes,))))
        self.alpha_p = torch.nn.Parameter(0.001 * torch.exp(torch.randn((n_genes,))))
        self.sigma = torch.nn.Parameter(1.0 * torch.exp(torch.randn((n_genes,))))
        self.sigma_p = torch.nn.Parameter(1.0 * torch.exp(torch.randn((n_genes,))))

        if learn_T:
            self.T = torch.nn.Parameter(torch.tensor(T))
        else:
            self.T = torch.tensor(T)  # torch.nn.Parameter(torch.tensor(1.0))

        if self.lyapunov_penalty:
            # covariance_matrix = cov_factor @ cov_factor.T + cov_diag
            # The computation for determinant and inverse of covariance matrix is
            # avoided when cov_factor.shape[1] << cov_factor.shape[0], see pytorch
            # docs
            self.w_cov_diag = torch.nn.Parameter(
                torch.exp(
                    0.1
                    * torch.rand(
                        (
                            self.n_contexts,
                            n_genes,
                        )
                    )
                )
            )
            self.w_cov_factor = torch.nn.Parameter(
                0.1 * torch.randn((self.n_contexts, n_genes, rank_w_cov_factor))
            )

        if init_tensors is not None:
            with torch.no_grad():
                print("Initializing parameters from data")
                if "alpha" in self.init_tensors:
                    self.alpha.data = self.init_tensors["alpha"]
                if "w_cov_factor" in self.init_tensors:
                    self.w_cov_factor.data = self.init_tensors["w_cov_factor"]
                if "w_cov_diag" in self.init_tensors:
                    self.w_cov_diag.data = self.init_tensors["w_cov_diag"]

        if gt_beta is not None:
            self.gt_beta = gt_beta
        if train_gene_ko is not None:
            self.train_gene_ko = train_gene_ko
        if test_gene_ko is not None:
            self.test_gene_ko = test_gene_ko

        # Preprocess covariates
        if covariates is not None:

            print("Covariates shape:", covariates.shape)

            n_covs = covariates.shape[1]

            print("n_cov:", n_covs)

            print("NOT DOING ANY PREPROCESSING OF COVARIATES RIGHT NOW...")
            print("MAKE SURE TO ROLL THIS BACK LATER...")
            # Remove covariates with zero variance
            # covariates = covariates[:, covariates.std(axis=0) > 0]
            # Orthonormalize covariates
            # print("- Orthonormalizing covariates... Please run OHE before passing categorical covariates.")
            # covariates = covariates - torch.mean(covariates, axis=0)
            # covariates = covariates / ( torch.mean(covariates**2, axis=0) ** 0.5 + 1e-15 )

            # if n_covs > 1:
            #    covariates = torch.pca_lowrank(covariates, q=n_covs, center=False, niter=50)[0]

            self.covariates = covariates
            self.cov_coefficients = torch.nn.Parameter(0.1 * torch.randn((n_covs, n_genes)))
        else:
            self.covariates = None

    def configure_optimizers(self):
        print("Using optimizer_kwargs:", self.optimizer_kwargs)
        if self.optimizer == "adam":
            return optim.Adam(self.parameters(), lr=self.lr, **self.optimizer_kwargs)
        elif self.optimizer == "rmsprop":
            return optim.RMSprop(self.parameters(), lr=self.lr, **self.optimizer_kwargs)
        elif self.optimizer == "adamlrs":
            optmsr = optim.Adam(self.parameters(), lr=self.lr, **self.optimizer_kwargs)
            scheduler = optim.lr_scheduler.ReduceLROnPlateau(
                optmsr, mode="min", factor=0.2, patience=10, min_lr=5e-5
            )
            # FIXME: CHECK IF THIS IS THE CORRECT LOSS
            return {
                "optimizer": optmsr,
                "lr_scheduler": scheduler,
                "monitor": "train_loss",
            }

    def get_updated_states(self):
        iv_a = (1 - self.gt_interv).T

        if self.mask is not None:
            self.beta = torch.zeros((self.n_genes, self.n_genes), device=self.device)
            self.beta[self.beta_idx[0], self.beta_idx[1]] = self.beta_val.to(self.device)
            self.beta_p = torch.zeros((self.n_genes, self.n_genes), device=self.device)
            self.beta_p[self.beta_idx[0], self.beta_idx[1]] = self.beta_p_val.to(self.device)

        iv_a = iv_a.to(self.device)

        if self.n_factors == 0:
            if self.intervention_type == "dCas9":
                betas = iv_a[:, None, :] * self.beta.to(self.device) + (1 - iv_a)[
                    :, None, :
                ] * self.beta_p.to(self.device)
            elif self.intervention_type == "Cas9":
                betas = iv_a[:, :, None] * self.beta.to(self.device) + (1 - iv_a)[
                    :, :, None
                ] * self.beta_p.to(self.device)
            else:
                raise NotImplementedError("Currently only Cas9 and dCas9 are supported as intervention_type.")
        else:
            if self.mask is None:
                if self.intervention_type == "dCas9":
                    factor2genes = iv_a[:, None, :] * self.factor2gene.to(self.device) + (1 - iv_a)[
                        :, None, :
                    ] * self.factor2gene_p.to(self.device)
                    betas = torch.einsum("bij,bjk->bik", self.gene2factor[None, :, :], factor2genes)
                    betas_diag = torch.diagonal(betas, offset=0, dim1=-2, dim2=-1)
                    betas_diag[:] = 0.0

                    self.beta = torch.einsum("ij,jk->ik", self.gene2factor, self.factor2gene)
                    beta_diag = torch.diagonal(self.beta, offset=0, dim1=-2, dim2=-1)
                    beta_diag[:] = 0
                elif self.intervention_type == "Cas9":
                    gene2factors = iv_a[:, :, None] * self.gene2factor.to(self.device) + (1 - iv_a)[
                        :, :, None
                    ] * self.gene2factor_p.to(self.device)
                    betas = torch.einsum("bij,bjk->bik", gene2factors, self.factor2gene[None, :, :])
                    betas_diag = torch.diagonal(betas, offset=0, dim1=-2, dim2=-1)
                    betas_diag[:] = 0.0

                    self.beta = torch.einsum("ij,jk->ik", self.gene2factor, self.factor2gene)
                    beta_diag = torch.diagonal(self.beta, offset=0, dim1=-2, dim2=-1)
                    beta_diag[:] = 0
                else:
                    raise NotImplementedError(
                        "Currently only Cas9 and dCas9 are supported as intervention_type."
                    )
            else:
                raise NotImplementedError("Combination of factorization and masking not implemented yet.")

        if self.intervention_type == "dCas9":
            alphas = (
                iv_a * self.pos(self.alpha.to(self.device))[None, :]
                + (1 - iv_a) * self.pos(self.alpha_p.to(self.device))[None, :]
            )

            sigmas = iv_a[:, None, :] * torch.diag(self.pos(self.sigma) + self.sigma_min) + (1 - iv_a)[
                :, None, :
            ] * torch.diag(self.pos(self.sigma_p) + self.sigma_min)
        elif self.intervention_type == "Cas9":
            alphas = self.pos(self.alpha.to(self.device))[None, :].expand(iv_a.shape[0], self.alpha.shape[0])
            sigmas = torch.diag(self.pos(self.sigma) + self.sigma_min)[None, :, :].expand(
                iv_a.shape[0], self.sigma.shape[0], self.sigma.shape[0]
            )
        else:
            raise NotImplementedError("Currently only Cas9 and dCas9 are supported as intervention_type.")

        B = torch.eye(self.n_genes, device=self.device)[None, :, :] - (
            1.0 - torch.eye(self.n_genes, device=self.device)
        )[None, :, :] * betas.transpose(1, 2)

        return alphas, betas, B, sigmas

    def lyapunov_lhs(self, B):
        mat = B @ (
            torch.diag_embed(self.pos(self.w_cov_diag) + self.sigma_min)
            + self.w_cov_factor @ self.w_cov_factor.transpose(1, 2)
        )
        return mat + mat.transpose(1, 2)

    def lyapunov_rhs(self, sigmas):
        return torch.bmm(sigmas, sigmas.transpose(1, 2))

    # def compute_normalisations(self, log_likelihood, z_kl, l1_loss, spectral_loss, loss_lyapunov):
    #     # Normalise losses
    #     if self.normalise:
    #         if (self.current_epoch == 0) & (self._normalise_llh is None) & self.training:
    #             # save normalization constants
    #             self._normalise_llh = log_likelihood.detach().abs()
    #             self._normalise_kl = z_kl.detach().abs()
    #             self._normalise_l1 = l1_loss.detach().abs()
    #             if self.scale_spectral > 0:
    #                 self._normalise_spectral_loss = spectral_loss.detach().abs()
    #             if self.scale_lyapunov > 0:
    #                 self._normalise_lyapunov = loss_lyapunov.detach().abs()
    #     else:
    #         self._normalise_llh = 1.0
    #         self._normalise_kl = 1.0
    #         self._normalise_l1 = 1.0
    #         self._normalise_spectral_loss = 1.0
    #         self._normalise_lyapunov = 1.0

    #     self._normalisation_computed = True

    def scale_losses(self, z_kl, l1_loss, loss_spectral=None, loss_lyapunov=None):
        l1_loss = self.scale_l1 * l1_loss
        z_kl = self.scale_kl * z_kl

        if loss_spectral:
            loss_spectral = self.scale_spectral * loss_spectral
        if loss_lyapunov:
            loss_lyapunov = self.scale_lyapunov * loss_lyapunov

        return z_kl, l1_loss, loss_spectral, loss_lyapunov

    def split_samples(self, samples, sim_regime, sample_idx, data_category):
        # Split all rows according to is_valid_data
        samples_train = samples[data_category == 0]
        sim_regime_train = sim_regime[data_category == 0]
        sample_idx_train = sample_idx[data_category == 0]
        samples_valid = samples[data_category == 1]
        sim_regime_valid = sim_regime[data_category == 1]
        sample_idx_valid = sample_idx[data_category == 1]
        samples_test = samples[data_category == 2]
        sim_regime_test = sim_regime[data_category == 2]
        sample_idx_test = sample_idx[data_category == 2]

        return (
            samples_train,
            sim_regime_train,
            sample_idx_train,
            samples_valid,
            sim_regime_valid,
            sample_idx_valid,
            samples_test,
            sim_regime_test,
            sample_idx_test,
        )

    def get_x_bar(self, B, alphas, sim_regime):
        # Broadcast arrays to batch_shape
        B_broadcasted = B[sim_regime]
        alphas_broadcasted = alphas[sim_regime]

        if B.shape[0] == 1:
            B_broadcasted = B_broadcasted.squeeze()
            alphas_broadcasted = alphas_broadcasted.squeeze()

        x_bar = torch.bmm(torch.linalg.inv(B_broadcasted), alphas_broadcasted[:, :, None]).squeeze()
        return x_bar

    def get_mvn_normal(self, B, alphas, sim_regime, sigmas):
        x_bar = self.get_x_bar(B, alphas, sim_regime)

        if self.lyapunov_penalty:
            if self.rank_w_cov_factor < self.n_genes:

                dists = torch.distributions.LowRankMultivariateNormal(
                    x_bar.double(),
                    self.w_cov_factor[sim_regime].double(),
                    self.pos(self.w_cov_diag)[sim_regime].double() + self.sigma_min,
                )

                dists.loc = dists.loc.float()
                dists.cov_factor = dists.cov_factor.float()
                dists.cov_diag = dists.cov_diag.float()

                dists._unbroadcasted_cov_factor = dists._unbroadcasted_cov_factor.float()
                dists._unbroadcasted_cov_diag = dists._unbroadcasted_cov_diag.float()
                dists._capacitance_tril = dists._capacitance_tril.float()

                return dists

            else:
                return torch.distributions.MultivariateNormal(
                    x_bar,
                    scale_tril=torch.diag_embed(self.pos(self.w_cov_diag)[sim_regime] + self.sigma_min)
                    + torch.tril(self.w_cov_factor, diagonal=-1)[sim_regime],
                )
        else:
            omegas = lyapunov_direct(
                B.double(),
                torch.bmm(sigmas, sigmas.transpose(1, 2)).double(),
            ).float()
            return torch.distributions.MultivariateNormal(x_bar, covariance_matrix=omegas[sim_regime])

    def _get_posterior_dist(self, sample_idx, samples, sim_regime):
        if self.use_encoder:
            gt_nonzeros = self.gt_nonzeros.to(self.device)

            if gt_nonzeros.shape[1] > 1:
                ohes = gt_nonzeros[:, sim_regime].T
            else:
                ohes = gt_nonzeros[:, sim_regime].squeeze().T

            m = torch.cat([samples, ohes], 1)
            mu, variance = self.encoder(m)
            return torch.distributions.MultivariateNormal(mu, torch.diag_embed(variance + self.sigma_min))
        else:
            z_locs = self.z_loc[sample_idx]
            z_scales = self.pos(self.z_scale[sample_idx])
            return torch.distributions.MultivariateNormal(z_locs, torch.diag_embed(z_scales + self.sigma_min))

    def compute_kl_divergence_loss(self, mvn_dist, sample_idx, samples, sim_regime):
        """Compute KL Divergence between prior and posterior distribution"""
        z_mvn = self._get_posterior_dist(sample_idx, samples, sim_regime)
        z_kl = kl_divergence(z_mvn, mvn_dist).mean()
        return z_kl

    def compute_nll_loss(self, samples, sample_idx, sim_regime, mvn=None):
        """Compute NLL Loss."""
        if self.use_latents:
            if self.x_distribution == "Poisson":
                zs = self._get_posterior_dist(sample_idx, samples, sim_regime).rsample()
                if self.covariates is not None:
                    zs = zs + torch.mm(self.covariates[sample_idx], self.cov_coefficients)

                library_size = samples[:, self.nll_mask].sum(axis=1).reshape(-1, 1)
                # FIXME: Figure out why softplus does not work, i.e. self.pos
                ps = torch.softmax(zs[:, self.nll_mask] / self.T, dim=-1)

                P = torch.distributions.poisson.Poisson(rate=library_size * ps)
            elif self.x_distribution == "Normal":
                z_locs = self.z_loc[sample_idx]
                z_scales = self.pos(self.z_scale[sample_idx])
                if self.covariates is not None:
                    z_locs = z_locs + torch.mm(self.covariates[sample_idx], self.cov_coefficients)
                P = torch.distributions.normal.Normal(
                    loc=z_locs[:, self.nll_mask], scale=z_scales[:, self.nll_mask]
                )

            elif self.x_distribution == "NormalNormal":
                z_locs = self.z_loc[sample_idx]
                z_scales = self.pos(self.z_scale[sample_idx])
                P_z = torch.distributions.normal.Normal(loc=z_locs, scale=z_scales).rsample()
                if self.covariates is not None:
                    P_z = P_z + torch.mm(self.covariates[sample_idx], self.cov_coefficients)
                P = torch.distributions.normal.Normal(
                    loc=P_z[:, self.nll_mask], scale=self.x_distribution_kwargs.get("scale", 0.1)
                )
            elif self.x_distribution == "Multinomial":
                zs = self._get_posterior_dist(sample_idx, samples, sim_regime).rsample()
                if self.covariates is not None:
                    zs = zs + torch.mm(self.covariates[sample_idx], self.cov_coefficients)
                P = torch.distributions.multinomial.Multinomial(
                    logits=zs[:, self.nll_mask] / self.T, validate_args=False
                )

            return -1 * P.log_prob(samples[:, self.nll_mask]).mean()

        else:
            # TODO: In this case we could regress out the covariates already in the beginning
            # However, this would require to modify the data loader
            if self.covariates is not None:
                samples = samples - torch.mm(self.covariates[sample_idx], self.cov_coefficients)
            return -1 * mvn.log_prob(samples).mean()

    def compute_spectral_loss(self, B):
        """Compute Spectral Loss"""
        # FIXME: Think about the sign...
        # Note: I don't really need the transpose...
        real_eigval_max_c = torch.max(torch.real(torch.linalg.eigvals(-B)), axis=1)[0]
        spectral_loss = torch.clip(real_eigval_max_c, min=-0.01).mean()
        return spectral_loss

    def training_step(self, batch, batch_idx):
        kwargs = {"on_step": False, "on_epoch": True}
        prefix = "train" if self.training else "valid"

        samples, sim_regime, sample_idx, data_category = batch

        if self.train_only_latents:
            # Treat all data as "training data", but
            # detach all parameters except for
            # latent scale and location
            data_category = 0 * data_category

        # Split all rows according to data_category
        (
            samples_train,
            sim_regime_train,
            sample_idx_train,
            samples_valid,
            sim_regime_valid,
            sample_idx_valid,
            samples_test,
            sim_regime_test,
            sample_idx_test,
        ) = self.split_samples(samples, sim_regime, sample_idx, data_category)

        # If samples is empty, we skip the training step
        if (len(samples_train) == 0) & self.training:
            return None

        alphas, _, B, sigmas = self.get_updated_states()

        if self.train_only_latents:
            # Treat all data as "training data", but
            # detach all parameters except for
            # latent scale and location
            alphas = alphas.detach()
            B = B.detach()
            sigmas = sigmas.detach()
            self.T = self.T.detach()

        #
        # Losses
        #

        # Spectral Loss (only for training data)
        if self.training & (self.scale_spectral > 0):
            loss_spectral = self.compute_spectral_loss(B)
        else:
            loss_spectral = None

        # Add Lyapunov Loss (only for training data)
        if self.lyapunov_penalty & self.training:
            loss_lyapunov = torch.sum(torch.square(self.lyapunov_lhs(B) - self.lyapunov_rhs(sigmas))) / (
                self.n_genes**2 * self.n_contexts
            )
        else:
            loss_lyapunov = None

        # Sparsity Loss
        # FIXME: Maybe we should not use the mask here?
        if self.mask is None:
            if self.n_factors == 0:
                loss_l1 = torch.abs(self.beta).mean()
            else:
                loss_l1 = 0.5 * (torch.abs(self.gene2factor).mean() + torch.abs(self.factor2gene).mean())
        else:
            if self.n_factors == 0:
                loss_l1 = torch.abs(self.beta_val).mean()
            else:
                raise NotImplementedError("Combination of factorization and masking not implemented yet.")

        # In case we face valid or test data, we have to detach some parameters that must not get an update
        if (len(samples_valid) > 0) | (len(samples_test) > 0):
            B_detached = B.detach()
            alphas_detached = alphas.detach()
            sigmas_detached = sigmas.detach()
        if len(samples_test) > 0:
            gt_interv = self.gt_interv.to(self.device)
            iv_a = (1 - gt_interv).T
            alphas_detached_masked = (alphas * (1.0 - iv_a)) + (alphas * iv_a).detach()
            sigmas_detached_masked = (sigmas * (1.0 - iv_a)[:, None, :]) + (
                sigmas * iv_a[:, None, :]
            ).detach()

        # KL Divergence & NLL Loss
        neg_log_likelihood = 0
        z_kl = 0
        if self.use_latents:
            if self.training:
                mvn_train = self.get_mvn_normal(B, alphas, sim_regime_train, sigmas)
                z_kl_train = self.compute_kl_divergence_loss(
                    mvn_train, sample_idx_train, samples_train, sim_regime_train
                )
                neg_log_likelihood_train = self.compute_nll_loss(
                    samples_train, sample_idx_train, sim_regime_train
                )
                self.log(f"{prefix}_kl_train", z_kl_train, **kwargs)
                self.log(f"{prefix}_nll_train", neg_log_likelihood_train, **kwargs)
                self.log(
                    f"{prefix}_sigma_min",
                    torch.diagonal(mvn_train.covariance_matrix, dim1=-2, dim2=-1).min().detach(),
                    **kwargs,
                )
                self.log(f"{prefix}_alpha_min", alphas.min().detach(), **kwargs)
                self.log(f"{prefix}_alpha_max", alphas.max().detach(), **kwargs)

                if self.covariates is not None:
                    self.log(f"{prefix}_cov_weight_mean", self.cov_coefficients.mean(), **kwargs)
                    self.log(f"{prefix}_cov_weight_min", self.cov_coefficients.min(), **kwargs)
                    self.log(f"{prefix}_cov_weight_max", self.cov_coefficients.max(), **kwargs)
                neg_log_likelihood += neg_log_likelihood_train
                z_kl += z_kl_train

            # Valid Data
            if len(samples_valid) > 0:
                # Block every gradient coming from the MVN
                mvn_valid = self.get_mvn_normal(
                    B_detached, alphas_detached, sim_regime_valid, sigmas_detached
                )
                z_kl_valid = self.compute_kl_divergence_loss(
                    mvn_valid, sample_idx_valid, samples_valid, sim_regime_valid
                )
                neg_log_likelihood_valid = self.compute_nll_loss(
                    samples_valid, sample_idx_valid, sim_regime_valid
                )
                self.log(f"{prefix}_kl_valid", z_kl_valid, **kwargs)
                self.log(f"{prefix}_nll_valid", neg_log_likelihood_valid, **kwargs)
                neg_log_likelihood += neg_log_likelihood_valid
                z_kl += z_kl_valid

            # Test Data
            if len(samples_test) > 0:
                # Block every gradient coming from the MVN via B, but keep gradients on alphas and sigmas
                mvn_test = self.get_mvn_normal(
                    B_detached,
                    alphas_detached_masked,
                    sim_regime_test,
                    sigmas_detached_masked,
                )
                z_kl_test = self.compute_kl_divergence_loss(
                    mvn_test, sample_idx_test, samples_test, sim_regime_test
                )
                neg_log_likelihood_test = self.compute_nll_loss(
                    samples_test, sample_idx_test, sim_regime_test
                )
                self.log(f"{prefix}_kl_test", z_kl_test, **kwargs)
                self.log(f"{prefix}_nll_test", neg_log_likelihood_test, **kwargs)
                neg_log_likelihood += neg_log_likelihood_test
                z_kl += z_kl_test

        else:
            if self.training:
                mvn_train = self.get_mvn_normal(B, alphas, sim_regime_train, sigmas)
                neg_log_likelihood_train = self.compute_nll_loss(
                    samples_train, sample_idx_train, sim_regime_train, mvn_train
                )
                self.log(f"{prefix}_nll_train", neg_log_likelihood_train, **kwargs)

                self.log(
                    f"{prefix}_sigma_min",
                    torch.diagonal(mvn_train.covariance_matrix, dim1=-2, dim2=-1).min().detach(),
                    **kwargs,
                )

                if self.covariates is not None:
                    self.log(f"{prefix}_cov_weight_mean", self.cov_coefficients.mean(), **kwargs)
                    self.log(f"{prefix}_cov_weight_min", self.cov_coefficients.min(), **kwargs)
                    self.log(f"{prefix}_cov_weight_max", self.cov_coefficients.max(), **kwargs)
                neg_log_likelihood += neg_log_likelihood_train

            # Valid Data
            if len(samples_valid) > 0:
                # Block every gradient coming from the MVN
                mvn_valid = self.get_mvn_normal(
                    B_detached, alphas_detached, sim_regime_valid, sigmas_detached
                )
                neg_log_likelihood_valid = self.compute_nll_loss(
                    samples_valid, sample_idx_valid, sim_regime_valid, mvn_valid
                )
                self.log(f"{prefix}_nll_valid", neg_log_likelihood_valid, **kwargs)
                neg_log_likelihood += neg_log_likelihood_valid

            # Test Data
            if len(samples_test) > 0:
                # Block every gradient coming from the MVN via B, but keep gradients on alphas and sigmas
                mvn_test = self.get_mvn_normal(
                    B_detached,
                    alphas_detached_masked,
                    sim_regime_test,
                    sigmas_detached_masked,
                )
                neg_log_likelihood_test = self.compute_nll_loss(
                    samples_test, sample_idx_test, sim_regime_test, mvn_test
                )
                self.log(f"{prefix}_nll_test", neg_log_likelihood_test, **kwargs)
                neg_log_likelihood += neg_log_likelihood_test

        #
        # Combine Losses
        #

        # Rescale combined KL divergence (train, valid and test) by number of genes
        z_kl = z_kl / self.n_genes

        # Apply rescaling also to NLLs which are not calculated on a per-gene basis:
        if self.x_distribution == "Multinomial":
            neg_log_likelihood = neg_log_likelihood / self.n_genes

        # Scale losses
        z_kl, loss_l1, loss_spectral, loss_lyapunov = self.scale_losses(
            z_kl, loss_l1, loss_spectral, loss_lyapunov
        )

        loss = neg_log_likelihood + loss_l1 + z_kl
        if self.training:
            if self.scale_spectral > 0:
                loss += loss_spectral
            if self.scale_lyapunov > 0:
                loss += loss_lyapunov

        self.log(f"{prefix}_loss", loss, **kwargs)
        self.log(f"{prefix}_l1", loss_l1, **kwargs)
        self.log(f"{prefix}_T", self.T, **kwargs)
        if self.training:
            if self.scale_spectral > 0:
                self.log(f"{prefix}_spectral_loss", torch.abs(loss_spectral), **kwargs)
            if self.scale_lyapunov > 0:
                self.log(f"{prefix}_lyapunov", loss_lyapunov, **kwargs)
        else:
            if self.early_stopping:
                self.validation_step_outputs.append(loss)

        if self.train_only_likelihood:
            loss = neg_log_likelihood

        return loss

    def validation_step(self, batch, batch_idx):
        self.training_step(batch, batch_idx)

    def predict_percentages(self, batch):

        samples, sim_regime, sample_idx, data_category = batch

        z_mvn = self._get_posterior_dist(sample_idx, samples, sim_regime)
        z_means = z_mvn.mean

        ps = torch.softmax(z_means / self.T, dim=-1)

        return ps

    def predict_step(self, batch, dataloader_idx=0):
        samples, sim_regime, sample_idx, _ = batch

        alphas, _, B, sigmas = self.get_updated_states()

        # KL Divergence & NLL Loss
        mvn_test = self.get_mvn_normal(
            B,
            alphas,
            sim_regime,
            sigmas,
        )
        if self.use_latents:
            z_kl = self.compute_kl_divergence_loss(mvn_test, sample_idx, samples, sim_regime)
        else:
            z_kl = 0

        neg_log_likelihood = self.compute_nll_loss(samples, sample_idx, sim_regime, mvn_test)

        # Rescale combined KL divergence
        z_kl = z_kl / self.n_genes

        # Apply rescaling also to NLLs which are not calculated on a per-gene basis:
        if self.x_distribution == "Multinomial":
            neg_log_likelihood = neg_log_likelihood / self.n_genes

        loss = neg_log_likelihood + z_kl

        return loss

    def predict_means(self, regimes=[]):

        if len(regimes) < 1 or np.asarray(regimes).max() >= self.n_contexts:
            print("List of regimes to predict not valid... skipping. List:", regimes)

        alphas, betas, B, sigmas = self.get_updated_states()
        sim_regime = torch.tensor(np.asarray(regimes), device=alphas.device).long()

        x_bar = self.get_x_bar(B, alphas, sim_regime)

        return x_bar

    def predict_perturbation(self, target_idx, target_mu = [], target_std = [], max_epochs = 1000, predict_omega = True):
   
        self.gt_interv.to(self.device)

        # first: calculate values for alpha_p and sigma_p for the given targets
        # c.f. stationary distribution of univariate Ornstein-Uhlenbeck process,
        # e.g., here: https://en.wikipedia.org/wiki/Ornstein%E2%80%93Uhlenbeck_process
        sigma_p = math.sqrt(2.0)*torch.tensor(np.asarray(target_std),device = self.device, dtype = self.sigma.dtype)
        alpha_p = torch.tensor(np.asarray(target_mu),device = self.device, dtype = self.alpha.dtype)
        
        gt_interv_orig = self.gt_interv
        
        self.gt_interv = torch.zeros((self.n_genes,1))
        self.gt_interv[target_idx,0] = 1
        self.gt_interv.to(gt_interv_orig.device)        
        alpha, beta, B, sigma = self.get_updated_states()
        self.gt_interv = gt_interv_orig
        
        alpha = alpha[0]
        beta = beta[0]
        sigma = sigma[0]
        B = B[0]
        sigma = torch.diagonal(sigma, offset=0, dim1=-2, dim2=-1)

        # In case use explicitly specified expected means and standard deviations
        # for LATENT expression, use these
        if len(target_mu) == len(target_idx) and len(target_std) == len(target_idx):
            alpha[target_idx] = alpha_p
            sigma[target_idx] = sigma_p
       
        sigma = torch.diag_embed(sigma)
       
        x_bar = torch.mm(torch.linalg.inv(B), alpha[:,None])

        if predict_omega:

          omega_model = Omega_Iterative(alpha, beta, B, sigma, device = self.device)
          
          # Empty dataloader, just so PL won't complain
          dataset = TensorDataset(torch.zeros((1,1)))
          dataloader = DataLoader(dataset)
          
          trainer = pl.Trainer(
              max_epochs=max_epochs,
              accelerator='cpu',  # if str(device).startswith("cuda") else "cpu",
              #devices=[GPU_DEVICE],  # if str(device).startswith("cuda") else 1,
              num_sanity_val_steps=0
          )
          
          start_time = time.time()    
          trainer.fit(omega_model, dataloader)    
          end_time = time.time()

          self.omega_model = omega_model

          omega = ( torch.diag_embed(omega_model.pos(omega_model.w_cov_diag) + omega_model.sigma_min)
                    + omega_model.w_cov_factor @ omega_model.w_cov_factor.transpose(0, 1) )

          return x_bar, omega

        else:

          return x_bar

    def forward(self):
        raise NotImplementedError()

    def on_validation_epoch_end(self):
        if self.early_stopping:
            avg_loss = torch.stack(self.validation_step_outputs).mean()
            self.log("avg_valid_loss", avg_loss)

            if self.earlystopper.step(avg_loss):
                print(f"Earlystopping due to convergence at step {self.current_epoch}")
                self.trainer.should_stop = True

            self.validation_step_outputs.clear()

    def on_fit_end(self):
        self.is_fitted = True
