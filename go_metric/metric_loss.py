import torch
from torch import nn

def get_all_triplets(sim_score, sim_margin=3.0):
    matches = sim_score > sim_margin
    diffs = sim_score <= sim_margin
    triplets = matches.unsqueeze(2) * diffs.unsqueeze(1)
    return torch.where(triplets)

def multilabel_triplet_loss(embeddings, labels, label_weights=None, sim_margin=1.0, tmargin=1.5):
    if(label_weights is None):
        label_weights = torch.ones((1, labels.shape[1]), device=labels.device)
    else:
        label_weights = label_weights.to(labels.device)
    emb_dist = torch.cdist(embeddings, embeddings)
    sim_score = labels.multiply(label_weights) @ labels.T
    a, p, n = get_all_triplets(sim_score, sim_margin=sim_margin)
    # print(a.shape)
    pos_pairs = emb_dist[a, p]
    neg_pairs = emb_dist[a, n]
    triplet_margin = neg_pairs - pos_pairs
    return torch.mean(torch.relu(tmargin - triplet_margin))

def contrastive_softmax_loss(embeddings, labels, label_weights=None, temperature=1.0):
    """
    embeddings: (batch_size x emb_dim) tensor
    labels: (batch_size x labels) tensor
    """
    nz = torch.nonzero(labels, as_tuple=True)
    active_emb = embeddings[nz[0]]
    active_labels = nz[1]
    al = torch.unsqueeze(active_labels, 0)
    mask = torch.eq(al.T, al)

    num_active = active_labels.shape[0]
    diag = torch.eye(num_active, dtype=torch.bool, device=embeddings.device)
    p_mask = mask & ~diag
    n_mask = ~mask & ~diag

    logits = active_emb @ active_emb.T / temperature
    max_log = logits.detach().max(axis=1, keepdim=True)
    logits -= max_log[0]
    exp_logits = torch.exp(logits)

    pos_per_row = p_mask.sum(axis=1)
    denominator = (p_mask*exp_logits).sum(axis=1, keepdim=True) + (n_mask*exp_logits).sum(axis=1, keepdim=True)

    log_probs = (logits - torch.log(denominator))*p_mask
    log_probs = log_probs.sum(axis=1)
    log_probs = log_probs / (pos_per_row + 0.001)
    loss = -log_probs
    return loss.mean()

class BarlowTwinsLoss(nn.Module):
    def __init__(self, batch_size, lambda_coeff=5e-3, z_dim=128):
        super().__init__()

        self.z_dim = z_dim
        self.batch_size = batch_size
        self.lambda_coeff = lambda_coeff

    def off_diagonal_ele(self, x):
        # taken from: https://github.com/facebookresearch/barlowtwins/blob/main/main.py
        # return a flattened view of the off-diagonal elements of a square matrix
        n, m = x.shape
        assert n == m
        return x.flatten()[:-1].view(n - 1, n + 1)[:, 1:].flatten()

    def forward(self, z1, z2):
        # N x D, where N is the batch size and D is output dim of projection head
        z1_norm = (z1 - torch.mean(z1, dim=0)) / torch.std(z1, dim=0)
        z2_norm = (z2 - torch.mean(z2, dim=0)) / torch.std(z2, dim=0)

        cross_corr = torch.matmul(z1_norm.T, z2_norm) / self.batch_size

        on_diag = torch.diagonal(cross_corr).add_(-1).pow_(2).sum()
        off_diag = self.off_diagonal_ele(cross_corr).pow_(2).sum()

        return on_diag + self.lambda_coeff * off_diag