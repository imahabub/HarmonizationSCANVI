from typing import Sequence

import numpy as np
import torch
from torch.distributions import Normal, Categorical, kl_divergence as kl

from scvi.models.classifier import Classifier
from scvi.models.modules import Decoder, Encoder
from scvi.models.utils import broadcast_labels
from scvi.models.vae import VAE


class SCANVI(VAE):
    r"""A semi-supervised Variational auto-encoder model - inspired from M1 + M2 model,
    as described in (https://arxiv.org/pdf/1406.5298.pdf). SCANVI stands for single-cell annotation using
    variational inference.

    :param n_input: Number of input genes
    :param n_batch: Number of batches
    :param n_labels: Number of labels
    :param n_hidden: Number of nodes per hidden layer
    :param n_latent: Dimensionality of the latent space
    :param n_layers: Number of hidden layers used for encoder and decoder NNs
    :param dropout_rate: Dropout rate for neural networks
    :param dispersion: One of the following

        * ``'gene'`` - dispersion parameter of NB is constant per gene across cells
        * ``'gene-batch'`` - dispersion can differ between different batches
        * ``'gene-label'`` - dispersion can differ between different labels
        * ``'gene-cell'`` - dispersion can differ for every gene in every cell

    :param log_variational: Log variational distribution
    :param reconstruction_loss:  One of

        * ``'nb'`` - Negative binomial distribution
        * ``'zinb'`` - Zero-inflated negative binomial distribution

    :param y_prior: If None, initialized to uniform probability over cell types
    :param labels_groups: Label group designations
    :param use_labels_groups: Whether to use the label groups

    Examples:
        >>> gene_dataset = CortexDataset()
        >>> scanvi = SCANVI(gene_dataset.nb_genes, n_batch=gene_dataset.n_batches * False,
        ... n_labels=gene_dataset.n_labels)

        >>> gene_dataset = SyntheticDataset(n_labels=3)
        >>> scanvi = SCANVI(gene_dataset.nb_genes, n_batch=gene_dataset.n_batches * False,
        ... n_labels=3, y_prior=torch.tensor([[0.1,0.5,0.4]]), labels_groups=[0,0,1])
    """

    def __init__(self, n_input: int, n_batch: int = 0, n_labels: int = 0,
                 n_hidden: int = 128, n_latent: int = 10, n_layers: int = 1,
                 dropout_rate: float = 0.1, dispersion: str = "gene",
                 log_variational: bool = True, reconstruction_loss: str = "zinb",
                 y_prior=None, labels_groups: Sequence[int] = None, use_labels_groups: bool = False,
                 classifier_parameters: dict = dict()):
        super(SCANVI, self).__init__(n_input, n_hidden=n_hidden, n_latent=n_latent, n_layers=n_layers,
                                     dropout_rate=dropout_rate, n_batch=n_batch, dispersion=dispersion,
                                     log_variational=log_variational, reconstruction_loss=reconstruction_loss)

        self.n_labels = n_labels
        self.n_latent_layers = 2
        # Classifier takes n_latent as input
        cls_parameters = {"n_layers": n_layers, "n_hidden": n_hidden, "dropout_rate": dropout_rate}
        cls_parameters.update(classifier_parameters)
        self.classifier = Classifier(n_latent, n_labels=n_labels, **cls_parameters)

        self.encoder_z2_z1 = Encoder(n_latent, n_latent, n_cat_list=[self.n_labels], n_layers=n_layers,
                                     n_hidden=n_hidden, dropout_rate=dropout_rate)
        self.decoder_z1_z2 = Decoder(n_latent, n_latent, n_cat_list=[self.n_labels], n_layers=n_layers,
                                     n_hidden=n_hidden)

        self.y_prior = torch.nn.Parameter(
            y_prior if y_prior is not None else (1 / n_labels) * torch.ones(1, n_labels), requires_grad=False
        )
        self.use_labels_groups = use_labels_groups
        self.labels_groups = np.array(labels_groups) if labels_groups is not None else None
        if self.use_labels_groups:
            assert labels_groups is not None, "Specify label groups"
            unique_groups = np.unique(self.labels_groups)
            self.n_groups = len(unique_groups)
            assert (unique_groups == np.arange(self.n_groups)).all()
            self.classifier_groups = Classifier(n_latent, n_hidden, self.n_groups, n_layers, dropout_rate)
            self.groups_index = torch.nn.ParameterList([torch.nn.Parameter(
                torch.tensor((self.labels_groups == i).astype(np.uint8), dtype=torch.uint8), requires_grad=False
            ) for i in range(self.n_groups)])

    def classify(self, x):
        if self.log_variational:
            x = torch.log(1 + x)
        qz_m, _, z = self.z_encoder(x)
        z = qz_m  # We classify using the inferred mean parameter of z_1 in the latent space
        if self.use_labels_groups:
            w_g = self.classifier_groups(z)
            unw_y = self.classifier(z)
            w_y = torch.zeros_like(unw_y)
            for i, group_index in enumerate(self.groups_index):
                unw_y_g = unw_y[:, group_index]
                w_y[:, group_index] = unw_y_g / (unw_y_g.sum(dim=-1, keepdim=True) + 1e-8)
                w_y[:, group_index] *= w_g[:, [i]]
        else:
            w_y = self.classifier(z)
        return w_y

    def regenerate_from_fixed_info(self, sample_batch, fixed_batch, fixed_cell_type, n_samples):
        batch_index = torch.cuda.IntTensor(sample_batch.shape[0], 1).fill_(fixed_batch)
        library = torch.cuda.FloatTensor(sample_batch.shape[0], 1).fill_(4)
        cell_type = torch.cuda.FloatTensor(sample_batch.shape[0], 1).fill_(fixed_cell_type)

        # get z2
        if self.log_variational:
            sample_batch = torch.log(1 + sample_batch)
        qz_m, qz_v, z = self.z_encoder(sample_batch)
        qz2_m, qz2_v, z2 = self.encoder_z2_z1(z, cell_type)

        if n_samples > 1:
            qz2_m = qz2_m.unsqueeze(0).expand((n_samples, qz2_m.size(0), qz2_m.size(1)))
            qz2_v = qz2_v.unsqueeze(0).expand((n_samples, qz2_v.size(0), qz2_v.size(1)))
            z2 = Normal(qz2_m, qz2_v.sqrt()).sample()
            # flatten for injecting
            z2 = z2.view((-1, qz2_m.size(2)))
            batch_index = torch.cuda.IntTensor(z2.shape[0], 1).fill_(fixed_batch)
            library = torch.cuda.FloatTensor(z2.shape[0], 1).fill_(4)
            cell_type = torch.cuda.FloatTensor(z2.shape[0], 1).fill_(fixed_cell_type)

        # and then inject
        pz1_m, pz1_v = self.decoder_z1_z2(z2, cell_type)
        px_scale, _, _, _ = self.decoder('gene', pz1_m, library, batch_index)

        # reshape
        if n_samples > 1:
            px_scale = px_scale.view((n_samples, -1, px_scale.size(1)))
        return px_scale

    def generate_latent_samples(self, cell_type, batch_info, n_samples):
        shape_u = (n_samples, self.n_latent)
        shape_c = (n_samples, 1)

        library = torch.ones(shape_c).fill_(4.).cuda()
        batch_index = torch.ones(shape_c).fill_(batch_info).cuda()
        y = torch.ones(shape_c).fill_(cell_type).cuda()
        u = Normal(torch.zeros(shape_u).cuda(), torch.ones(shape_u).cuda()).sample()
        pz1_m, pz1_v = self.decoder_z1_z2(u, y)
        z = Normal(pz1_m, torch.sqrt(pz1_v)).sample()

        px_scale, _, _, _ = self.decoder(self.dispersion, z, library, batch_index, y)
        return px_scale

    def get_latents(self, x, y=None):
        zs = super(SCANVI, self).get_latents(x)
        qz2_m, qz2_v, z2 = self.encoder_z2_z1(zs[0], y)
        if not self.training:
            z2 = qz2_m
        return [zs[0], z2]

    def forward(self, x, local_l_mean, local_l_var, batch_index=None, y=None):
        is_labelled = False if y is None else True

        px_scale, px_r, px_rate, px_dropout, qz1_m, qz1_v, z1, ql_m, ql_v, library = self.inference(x, batch_index, y)

        # Enumerate choices of label
        ys, z1s = (
            broadcast_labels(
                y, z1, n_broadcast=self.n_labels
            )
        )
        qz2_m, qz2_v, z2 = self.encoder_z2_z1(z1s, ys)
        pz1_m, pz1_v = self.decoder_z1_z2(z2, ys)

        reconst_loss = self._reconstruction_loss(x, px_rate, px_r, px_dropout)

        # KL Divergence
        mean = torch.zeros_like(qz2_m)
        scale = torch.ones_like(qz2_v)

        kl_divergence_z2 = kl(Normal(qz2_m, torch.sqrt(qz2_v)), Normal(mean, scale)).sum(dim=1)
        loss_z1_unweight = - Normal(pz1_m, torch.sqrt(pz1_v)).log_prob(z1s).sum(dim=-1)
        loss_z1_weight = Normal(qz1_m, torch.sqrt(qz1_v)).log_prob(z1).sum(dim=-1)
        kl_divergence_l = kl(Normal(ql_m, torch.sqrt(ql_v)), Normal(local_l_mean, torch.sqrt(local_l_var))).sum(dim=1)

        if is_labelled:
            return reconst_loss + loss_z1_weight + loss_z1_unweight, kl_divergence_z2 + kl_divergence_l

        probs = self.classifier(z1)
        reconst_loss += (loss_z1_weight + ((loss_z1_unweight).view(self.n_labels, -1).t() * probs).sum(dim=1))

        kl_divergence = (kl_divergence_z2.view(self.n_labels, -1).t() * probs).sum(dim=1)
        kl_divergence += kl(Categorical(probs=probs),
                            Categorical(probs=self.y_prior.repeat(probs.size(0), 1)))
        kl_divergence += kl_divergence_l

        return reconst_loss, kl_divergence
