import numpy as np
import torch
from torch.autograd import Function, Variable
from torch.nn import Module
import torch.nn.functional as F
from torch.nn.modules.loss import _assert_no_grad

def log_softmax(x, axis):
    x = (x - x.max(axis=axis, keepdims=True))
    return x - np.log(np.sum(np.exp(x), axis=axis, keepdims=True))

def forward_pass(log_probs, labels, blank):

    T, U, _ = log_probs.shape
    alphas = np.zeros((T, U), dtype='f')

    for t in range(1, T):
        alphas[t, 0] = alphas[t-1, 0] + log_probs[t-1, 0, blank]

    for u in range(1, U):
        alphas[0, u] = alphas[0, u-1] + log_probs[0, u-1, labels[u-1]]
    for t in range(1, T):
        for u in range(1, U):
            no_emit = alphas[t-1, u] + log_probs[t-1, u, blank]
            emit = alphas[t, u-1] + log_probs[t, u-1, labels[u-1]]
            alphas[t, u] = np.logaddexp(emit, no_emit)

    loglike = alphas[T-1, U-1] + log_probs[T-1, U-1, blank]
    return alphas, loglike

def backward_pass(log_probs, labels, blank):

    T, U, _ = log_probs.shape
    betas = np.zeros((T, U), dtype='f')
    betas[T-1, U-1] = log_probs[T-1, U-1, blank]

    for t in reversed(range(T-1)):
        betas[t, U-1] = betas[t+1, U-1] + log_probs[t, U-1, blank]

    for u in reversed(range(U-1)):
        betas[T-1, u] = betas[T-1, u+1] + log_probs[T-1, u, labels[u]]

    for t in reversed(range(T-1)):
        for u in reversed(range(U-1)):
            no_emit = betas[t+1, u] + log_probs[t, u, blank]
            emit = betas[t, u+1] + log_probs[t, u, labels[u]]
            betas[t, u] = np.logaddexp(emit, no_emit)

    print(betas)
    return betas, betas[0, 0]

def compute_gradient(log_probs, alphas, betas, labels, blank):
    T, U, _ = log_probs.shape
    grads = np.full(log_probs.shape, -float("inf"))
    log_like = betas[0, 0]

    grads[T-1, U-1, blank] = alphas[T-1, U-1]

    grads[:T-1, :, blank] = alphas[:T-1, :] + betas[1:, :]
    for u, l in enumerate(labels):
        grads[:, u, l] = alphas[:, u] + betas[:, u+1]

    grads = np.exp(alphas[..., None] + betas[..., None] + log_probs - log_like) \
            - np.exp(grads + log_probs - log_like)
    return grads

def transduce(log_probs, labels, blank=0):
    """
    Args:
        log_probs: 3D array with shape
              [input len, output len + 1, vocab size]
        labels: 1D array with shape [output time steps]
    Returns:
        float: The negative log-likelihood
        3D array: Gradients with respect to the
                    unnormalized input actications
    """
    alphas, ll_forward = forward_pass(log_probs, labels, blank)
    betas, ll_backward = backward_pass(log_probs, labels, blank)
    grads = compute_gradient(log_probs, alphas, betas, labels, blank)
    return -ll_forward, grads

def transduce_batch(probs, labels, flen, glen, blank=0):
    log_probs = log_softmax(probs, axis=3) # NOTE apply log softmax
    grads = np.zeros_like(log_probs)
    costs = []
    # TODO parallel loop
    for b in range(log_probs.shape[0]):
        t = int(flen[b])
        u = int(glen[b]) + 1
        ll, g = transduce(log_probs[b, :t, :u, :], labels[b, :u-1], blank)
        grads[b, :t, :u, :] = g
        costs.append(ll)
    return costs, grads

class _RNNT(Function):
    @staticmethod
    def forward(ctx, trans_acts, pred_acts, labels, act_lens, label_lens):
        is_cuda = True if trans_acts.is_cuda else False
        acts = trans_acts.unsqueeze(dim=2) + pred_acts.unsqueeze(dim=1)
        # grad to activations
        costs, grads = transduce_batch(acts.cpu().numpy(), labels.cpu().numpy(),
                            act_lens.cpu().numpy(), label_lens.cpu().numpy())

        costs = torch.FloatTensor([sum(costs)])
        grads = torch.Tensor(grads)
        if is_cuda:
            costs = costs.cuda()
            grads = grads.cuda()
        
        ctx.grads = Variable(grads.sum(dim=2)), Variable(grads.sum(dim=1))
        return costs
    
    @staticmethod
    def backward(ctx, grad_output):
        return ctx.grads[0], ctx.grads[1], None, None, None

class RNNTLoss(Module):
    """
    Parameters:
        `size_average` (bool): normalize the loss by the batch size
                (default `False`)
        `blank_label` (int): default 0
    """
    def __init__(self):
        super(RNNTLoss, self).__init__()
        self.rnnt = _RNNT.apply
    
    def forward(self, trans_acts, pred_acts, labels, act_lens, label_lens):
        assert len(labels.size()) == 2
        _assert_no_grad(labels)
        _assert_no_grad(act_lens)
        _assert_no_grad(label_lens)
        return self.rnnt(trans_acts, pred_acts, labels, act_lens, label_lens)