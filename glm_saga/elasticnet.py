# Logging
import logging
import math
import os
import sys

# Toy example
import time
import warnings

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim import SGD
from torch.utils.data import DataLoader, Dataset, TensorDataset
from tqdm import tqdm


# Helper function for getting device of a module
def get_device(module):
    if hasattr(module, "device"):
        return module.device
    return next(module.parameters()).device


#######
# Solver assumes standardized input
class IndexedTensorDataset(TensorDataset):
    def __getitem__(self, index):
        val = super().__getitem__(index)
        return (*val, index)


class IndexedDataset(Dataset):
    def __init__(self, ds, sample_weight=None):
        super(Dataset, self).__init__()
        self.dataset = ds
        self.sample_weight = sample_weight

    def __getitem__(self, index):
        val = self.dataset[index]
        if self.sample_weight is None:
            return (*val, index)
        weight = self.sample_weight[index]
        return (*val, weight, index)

    def __len__(self):
        return len(self.dataset)

# L1 regularization
# proximal operator for f(\beta) = lam * \|\beta\|_1
def soft_threshold(beta, lam):
    return (beta - lam) * (beta > lam) + (beta + lam) * (beta < -lam)


# Grouped L1 regularization
# proximal operator for f(weight) = lam * \|weight\|_2
# where the 2-norm is taken columnwise
def group_threshold(weight, lam):
    norm = weight.norm(p=2, dim=0)
    return (weight - lam * weight / norm) * (norm > lam)


# Elastic net regularization
# proximal operator for f(x) = alpha * \|x\|_1 + beta * \|x\|_2^2
def soft_threshold_with_shrinkage(x, alpha, beta):
    y = soft_threshold(x, alpha)
    return y / (1 + beta)


# Elastic net regularization with group sparsity
# proximal operator for f(x) = alpha * \|x\|_1 + beta * \|x\|_2^2
# where the 2-norm is taken columnwise
def group_threshold_with_shrinkage(x, alpha, beta):
    y = group_threshold(x, alpha)
    return y / (1 + beta)


# Elastic net loss and accuracy
def elastic_loss_and_acc(linear, inputs, y, lam, alpha, family="multinomial"):
    weight, bias = list(linear.parameters())
    l1 = lam * alpha * weight.norm(p=1)
    l2 = 0.5 * lam * (1 - alpha) * (weight**2).sum()
    outputs = linear(inputs)
    if family == "multinomial":
        l = F.cross_entropy(outputs, y, reduction="mean")
        acc = (outputs.max(1)[1] == y).float().mean()
    elif family == "gaussian":
        l = 0.5 * F.mse_loss(outputs, y, reduction="mean")
        acc = (outputs == y).float().mean()
    else:
        raise ValueError(f"Unknown family {family}")

    loss = l + l1 + l2
    return loss, acc


# Elastic net loss given a loader instead
def elastic_loss_and_acc_loader(linear, loader, lam, alpha, preprocess=None, family="multinomial"):
    loss = 0
    acc = 0
    n = 0
    device = linear.weight.device
    for batch in loader:
        inputs, y = batch[0].to(device), batch[1].to(device)
        if preprocess is not None:
            inputs = preprocess(inputs)
        bs = inputs.size(0)
        l, a = elastic_loss_and_acc(linear, inputs, y, lam, alpha, family=family)
        loss += l * bs
        acc += a * bs
        n += bs
    return loss / n, acc / n


# Train an elastic GLM with proximal gradient as a baseline
def train(linear, inputs, y, lr, niters, lam, alpha, group=True, verbose=None):
    weight, bias = list(linear.parameters())

    opt = SGD(linear.parameters(), lr=lr)
    for i in range(niters):
        with torch.enable_grad():
            out = linear(inputs)
            loss = F.cross_entropy(out, y, reduction="mean") + 0.5 * lam * (1 - alpha) * (weight**2).sum()
            if verbose and (i % verbose) == 0:
                print(loss.item())

            # gradient step
            opt.zero_grad()
            loss.backward()
            opt.step()

        # proximal step
        if group:
            weight.data = group_threshold(weight, lr * lam * alpha)
        else:
            weight.data = soft_threshold(weight, lr * lam * alpha)


# Train an elastic GLM with proximal SAGA
# Since SAGA stores a scalar for each example-class pair, either pass
# the number of examples and number of classes or calculate it with an
# initial pass over the loaders
def train_saga(
    linear,
    loader,
    lr,
    num_epochs,
    lam,
    alpha,
    group=True,
    verbose=None,
    state=None,
    table_device=None,
    n_ex=None,
    num_classes=None,
    tol=1e-4,
    preprocess=None,
    lookbehind=None,
    family="multinomial",
    logger=None,
):
    if logger is None:
        logger = print
    with torch.no_grad():
        weight, bias = list(linear.parameters())
        if table_device is None:
            table_device = weight.device

        # get total number of examples and initialize scalars
        # for computing the gradients
        if n_ex is None:
            n_ex = sum(tensors[0].size(0) for tensors in loader)
        if num_classes is None:
            if family == "multinomial":
                num_classes = max(tensors[1].max().item() for tensors in loader) + 1
            elif family == "gaussian":
                for batch in loader:
                    y = batch[1]
                    break
                num_classes = y.size(1)

        # Storage for scalar gradients and averages
        if state is None:
            a_table = torch.zeros(n_ex, num_classes).to(table_device)
            w_grad_avg = torch.zeros_like(weight).to(weight.device)
            b_grad_avg = torch.zeros_like(bias).to(weight.device)
        else:
            a_table = state["a_table"].to(table_device)
            w_grad_avg = state["w_grad_avg"].to(weight.device)
            b_grad_avg = state["b_grad_avg"].to(weight.device)

        obj_history = []
        obj_best = None
        nni = 0
        for t in range(num_epochs):
            total_loss = 0
            for batch in loader:
                if len(batch) == 3:
                    inputs, y, idx = batch
                    w = None
                elif len(batch) == 4:
                    inputs, y, w, idx = batch
                else:
                    raise ValueError(
                        f"Loader must return (data, target, index) or (data, target, index, weight) but instead got a tuple of length {len(batch)}"
                    )

                if preprocess is not None:
                    device = get_device(preprocess)
                    with torch.no_grad():
                        inputs = preprocess(inputs.to(device))
                inputs = inputs.to(weight.device)
                out = linear(inputs)

                # split gradient on only the cross entropy term
                # for efficient storage of gradient information
                if family == "multinomial":
                    if w is None:
                        loss = F.cross_entropy(out, y.to(weight.device), reduction="mean")
                    else:
                        loss = F.cross_entropy(out, y.to(weight.device), reduction="none")
                        loss = (loss * w).mean()
                    I = torch.eye(linear.weight.size(0))
                    target = I[y].to(weight.device)  # change to OHE

                    # Calculate new scalar gradient
                    logits = F.softmax(linear(inputs), -1)
                elif family == "gaussian":
                    if w is None:
                        loss = 0.5 * F.mse_loss(out, y.to(weight.device), reduction="mean")
                    else:
                        loss = 0.5 * F.mse_loss(out, y.to(weight.device), reduction="none")
                        loss = (loss * (w.unsqueeze(1))).mean()
                    target = y

                    # Calculate new scalar gradient
                    logits = linear(inputs)
                else:
                    raise ValueError(f"Unknown family: {family}")
                total_loss += loss.item() * inputs.size(0)

                # BS x NUM_CLASSES
                a = logits - target
                if w is not None:
                    a = a * w.unsqueeze(1)
                a_prev = a_table[idx].to(weight.device)

                # weight parameter
                w_grad = (a.unsqueeze(2) * inputs.unsqueeze(1)).mean(0)
                w_grad_prev = (a_prev.unsqueeze(2) * inputs.unsqueeze(1)).mean(0)
                w_saga = w_grad - w_grad_prev + w_grad_avg
                weight_new = weight - lr * w_saga

                if alpha == 1:
                    # Pure L1 regularization
                    if group:
                        weight_new = group_threshold(weight_new, lr * lam * alpha)
                    else:
                        weight_new = soft_threshold(weight_new, lr * lam * alpha)
                else:
                    # Elastic net regularization
                    if group:
                        weight_new = group_threshold_with_shrinkage(
                            weight_new, lr * lam * alpha, lr * lam * (1 - alpha)
                        )
                    else:
                        weight_new = soft_threshold_with_shrinkage(weight_new, lr * lam * alpha, lr * lam * (1 - alpha))

                # bias parameter
                b_grad = a.mean(0)
                b_grad_prev = a_prev.mean(0)
                b_saga = b_grad - b_grad_prev + b_grad_avg
                bias_new = bias - lr * b_saga

                # update table and averages
                a_table[idx] = a.to(table_device)
                w_grad_avg.add_((w_grad - w_grad_prev) * inputs.size(0) / n_ex)
                b_grad_avg.add_((b_grad - b_grad_prev) * inputs.size(0) / n_ex)

                if lookbehind is None:
                    dw = (weight_new - weight).norm(p=2)
                    db = (bias_new - bias).norm(p=2)
                    criteria = torch.sqrt(dw**2 + db**2)

                    if criteria.item() <= tol:
                        return {
                            "a_table": a_table.cpu(),
                            "w_grad_avg": w_grad_avg.cpu(),
                            "b_grad_avg": b_grad_avg.cpu(),
                        }

                weight.data = weight_new
                bias.data = bias_new

            saga_obj = total_loss / n_ex + lam * alpha * weight.norm(p=1) + 0.5 * lam * (1 - alpha) * (weight**2).sum()

            # save amount of improvement
            obj_history.append(saga_obj.item())
            if obj_best is None or saga_obj.item() + tol < obj_best:
                obj_best = saga_obj.item()
                nni = 0
            else:
                nni += 1

            # Stop if no progress for lookbehind iterationsd:])
            criteria = lookbehind is not None and (nni >= lookbehind)

            nnz = (weight.abs() > 1e-5).sum().item()
            total = weight.numel()
            if verbose and (t % verbose) == 0:
                if lookbehind is None:
                    logger(
                        f"obj {saga_obj.item()} weight nnz {nnz}/{total} ({nnz / total:.4f}) criteria {criteria:.4f} {dw:.6f} {db:.6f}"
                    )
                else:
                    logger(f"obj {saga_obj.item()} weight nnz {nnz}/{total} ({nnz / total:.4f}) obj_best {obj_best}")

            if lookbehind is not None and criteria:
                logger(
                    f"obj {saga_obj.item()} weight nnz {nnz}/{total} ({nnz / total:.4f}) obj_best {obj_best} [early stop at {t}]"
                )
                return {
                    "a_table": a_table.cpu(),
                    "w_grad_avg": w_grad_avg.cpu(),
                    "b_grad_avg": b_grad_avg.cpu(),
                }

        logger(f"did not converge at {num_epochs} iterations (criteria {criteria})")
        return {
            "a_table": a_table.cpu(),
            "w_grad_avg": w_grad_avg.cpu(),
            "b_grad_avg": b_grad_avg.cpu(),
        }


# Same as before, but for a loader instead
def maximum_reg_loader(loader, group=True, preprocess=None, metadata=None, family="multinomial"):
    if metadata is not None:
        return metadata["max_reg"]["group"] if group else metadata["max_reg"]["nongrouped"]

    print("Calculating maximum regularization from dataloader...")
    # calculate number of classes
    y_max = 1
    for batch in loader:
        y = batch[1]
        y_max = max(y_max, y.max().item() + 1)

    if family == "multinomial":
        eye = torch.eye(y_max).to(y.device)

    y_bar = 0
    n = 0

    # calculate mean
    for batch in loader:
        y = batch[1]

        if family == "multinomial":
            target = eye[y]
        elif family == "gaussian":
            target = y
        else:
            raise ValueError(f"Unknown family {family}")

        y_bar += target.sum(0)
        n += y.size(0)
    y_bar = y_bar.float() / n

    # calculate std
    y_std = 0
    for batch in loader:
        y = batch[1]

        if family == "multinomial":
            target = eye[y]
        elif family == "gaussian":
            target = y
        else:
            raise ValueError(f"Unknown family {family}")

        y_std += ((target - y_bar) ** 2).sum(0)
    y_std = torch.sqrt(y_std.float() / (n - 1))

    # calculate maximum regularization
    inner_products = 0
    device = get_device(preprocess) if preprocess is not None else y.device
    for batch in loader:
        inputs, y = batch[0], batch[1]

        if family == "multinomial":
            target = eye[y]
        elif family == "gaussian":
            target = y
        else:
            raise ValueError(f"Unknown family {family}")

        y_map = target - y_bar

        if preprocess is not None:
            inputs = preprocess(inputs.to(device))
            y_map = y_map.to(device)
            y_std = y_std.to(device)
        inner_products += inputs.t().mm(y_map)

    if group:
        inner_products = inner_products.norm(p=2, dim=1)
    return inner_products.abs().max().item() / n


# Calculate the regularization path of an elastic GLM with proximal SAGA
# Returns a dictionary of <regularization parameter> -> <linear weights and optimizer state>
def glm_saga(
    linear,
    loader,
    max_lr,
    num_epochs,
    alpha,
    table_device=None,
    preprocess=None,
    group=False,
    verbose=None,
    state=None,
    n_ex=None,
    num_classes=None,
    tol=1e-4,
    epsilon=0.001,
    k=100,
    checkpoint=None,
    do_zero=True,
    lr_decay_factor=1,
    metadata=None,
    val_loader=None,
    test_loader=None,
    lookbehind=None,
    family="multinomial",
    encoder=None,
    max_sparsity=None,
):
    if encoder is not None:
        warnings.warn(
            "encoder argument is deprecated; please use preprocess instead",
            DeprecationWarning,
        )
        preprocess = encoder

    if preprocess is not None and (get_device(linear) != get_device(preprocess)):
        raise ValueError(
            "Linear and preprocess must be on same device (got {get_device(linear)} and {get_device(preprocess)})"
        )

    if metadata is not None:
        if n_ex is None:
            n_ex = metadata["inputs"]["num_examples"]
        if num_classes is None:
            num_classes = metadata["y"]["num_classes"]

    max_lam = maximum_reg_loader(loader, group=group, preprocess=preprocess, metadata=metadata, family=family) / max(
        0.001, alpha
    )
    min_lam = epsilon * max_lam

    # logspace is base 10 but log is base e so use log10
    lams = torch.logspace(math.log10(max_lam), math.log10(min_lam), k)
    lrs = torch.logspace(math.log10(max_lr), math.log10(max_lr / lr_decay_factor), k)

    if do_zero:
        lams = torch.cat([lams, lams.new_zeros(1)])
        lrs = torch.cat([lrs, lrs.new_ones(1) * lrs[-1]])

    path = []
    best_val_loss = float("inf")

    if checkpoint is not None:
        os.makedirs(checkpoint, exist_ok=True)

        file_handler = logging.FileHandler(filename=os.path.join(checkpoint, "output.log"))
        stdout_handler = logging.StreamHandler(sys.stdout)
        handlers = [file_handler, stdout_handler]

        logging.basicConfig(
            level=logging.DEBUG,
            format="[%(asctime)s] %(levelname)s - %(message)s",
            handlers=handlers,
        )
        logger = logging.getLogger("glm_saga").info
    else:
        logger = print

    for i, (lam, lr) in enumerate(zip(lams, lrs, strict=False)):
        start_time = time.time()

        state = train_saga(
            linear,
            loader,
            lr,
            num_epochs,
            lam,
            alpha,
            table_device=table_device,
            preprocess=preprocess,
            group=group,
            verbose=verbose,
            state=state,
            n_ex=n_ex,
            num_classes=num_classes,
            tol=tol,
            lookbehind=lookbehind,
            family=family,
            logger=logger,
        )

        with torch.no_grad():
            loss, acc = elastic_loss_and_acc_loader(linear, loader, lam, alpha, preprocess=preprocess, family=family)
            loss, acc = loss.item(), acc.item()

            loss_val, acc_val = -1, -1
            if val_loader:
                loss_val, acc_val = elastic_loss_and_acc_loader(
                    linear, val_loader, lam, alpha, preprocess=preprocess, family=family
                )
                loss_val, acc_val = loss_val.item(), acc_val.item()

            loss_test, acc_test = -1, -1
            if test_loader:
                loss_test, acc_test = elastic_loss_and_acc_loader(
                    linear,
                    test_loader,
                    lam,
                    alpha,
                    preprocess=preprocess,
                    family=family,
                )
                loss_test, acc_test = loss_test.item(), acc_test.item()

            params = {
                "lam": lam,
                "lr": lr,
                "alpha": alpha,
                "time": time.time() - start_time,
                "loss": loss,
                "metrics": {
                    "loss_tr": loss,
                    "acc_tr": acc,
                    "loss_val": loss_val,
                    "acc_val": acc_val,
                    "loss_test": loss_test,
                    "acc_test": acc_test,
                },
                "weight": linear.weight.detach().cpu().clone(),
                "bias": linear.bias.detach().cpu().clone(),
            }
            path.append(params)
            if loss_val is not None and loss_val < best_val_loss:
                best_val_loss = loss_val
                best_params = params

            nnz = (linear.weight.abs() > 1e-5).sum().item()
            total = linear.weight.numel()
            if family == "multinomial":
                logger(
                    f"({i}) lambda {lam:.4f}, loss {loss:.4f}, acc {acc:.4f} [val acc {acc_val:.4f}] [test acc {acc_test:.4f}], sparsity {nnz / total} [{nnz}/{total}], time {time.time() - start_time}, lr {lr:.4f}"
                )
            elif family == "gaussian":
                logger(
                    f"({i}) lambda {lam:.4f}, loss {loss:.4f} [val loss {loss_val:.4f}] [test loss {loss_test:.4f}], sparsity {nnz / total} [{nnz}/{total}], time {time.time() - start_time}, lr {lr:.4f}"
                )

            if checkpoint is not None:
                torch.save(params, os.path.join(checkpoint, f"params{i}.pth"))
            if max_sparsity is not None and nnz/total > max_sparsity:
                break
    return {"path": path, "best": best_params, "state": state}


# Given a loader, calculate the mean and standard deviation
# for normalization. If a model is provided, calculate the mean and
# standard deviation of the resulting representation obtained by
# first passing the example through the model.
class NormalizedRepresentation(nn.Module):
    def __init__(
        self,
        loader,
        model=None,
        do_tqdm=True,
        mean=None,
        std=None,
        metadata=None,
        device="cuda",
    ):
        super().__init__()

        self.model = model
        if model is not None:
            device = get_device(model)
        self.device = device

        if metadata is not None:
            X_bar = metadata["inputs"]["mean"]
            X_std = metadata["inputs"]["std"]
        else:
            if mean is None:
                # calculate mean
                X_bar = 0
                n = 0
                it = enumerate(loader)
                if do_tqdm:
                    it = tqdm(it, total=len(loader))

                for _, batch in it:
                    inputs = batch[0]
                    if model is not None:
                        inputs = model(inputs.to(device))

                    X_bar += inputs.sum(0)
                    n += inputs.size(0)

                X_bar = X_bar.float() / n
            else:
                X_bar = mean

            if std is None:
                X_std = 0
                it = enumerate(loader)
                if do_tqdm:
                    it = tqdm(it, total=len(loader))

                for _, batch in it:
                    inputs = batch[0]
                    if model is not None:
                        inputs = model(inputs.to(device))
                    X_std += ((inputs - X_bar) ** 2).sum(0)
                X_std = torch.sqrt(X_std / (n - 1))
            else:
                X_std = std

        self.mu = X_bar
        self.sigma = X_std

    def forward(self, inputs):
        if self.model is not None:
            device = get_device(self.model)
            inputs = self.model(inputs.to(device))
        return (inputs - self.mu.to(self.device)) / self.sigma.to(self.device)


class GLM:
    def __init__(
        self,
        num_classes: int,
        batch_size: int = 128,
        val_frac: float = 0.1,
        lr: float = 0.1,
        k: int = 100,
        max_epochs: int = 2000,
        alpha: float = 1,
        verbose: int = 200,
        group: bool = False,
        do_zero: bool = True,
        epsilon: float = 0.001,
        tol: float = 1e-4,
        max_sparsity: float | None = None
    ):
        self.num_classes = num_classes
        self.batch_size = batch_size
        self.val_frac = val_frac
        self.lr = lr
        self.max_epochs = max_epochs
        self.alpha = alpha
        self.verbose = verbose
        self.group = group
        self.epsilon = epsilon
        self.tol = tol

        self.k = k
        self.do_zero = do_zero
        self.max_sparsity = max_sparsity

    def fit(self, inputs: torch.Tensor, labels: torch.Tensor):
        val_sz = math.floor(inputs.shape[0] * self.val_frac)
        indices = torch.randperm(inputs.shape[0])

        X_val, X_tr = inputs[indices[:val_sz]], inputs[indices[val_sz:]]
        y_val, y_tr = labels[indices[:val_sz]], labels[indices[val_sz:]]

        ds_tr = IndexedTensorDataset(X_tr, y_tr)
        ds_val = TensorDataset(X_val, y_val)
        ld_tr = DataLoader(ds_tr, batch_size=self.batch_size, shuffle=True)
        ld_val = DataLoader(ds_val, batch_size=self.batch_size, shuffle=True)

        print("Initializing linear model...")
        self.linear = nn.Linear(X_tr.shape[1], self.num_classes).cuda()
        weight = self.linear.weight
        bias = self.linear.bias

        for p in [weight, bias]:
            p.data.zero_()

        print("Calculating the regularization path")
        self.params = glm_saga(
            linear = self.linear,
            loader= ld_tr,
            max_lr = self.lr,
            num_epochs=self.max_epochs,
            alpha=self.alpha,
            k = self.k,
            do_zero=self.do_zero,
            num_classes=self.num_classes,
            checkpoint=None,
            verbose=self.verbose,
            tol=self.tol,
            group=self.group,
            epsilon=self.epsilon,
            val_loader=ld_val,
            max_sparsity=self.max_sparsity
        )

    def get_params(self):
        return {"weight": self.linear.weight, "bias": self.linear.bias}

    @torch.no_grad
    def predict(self, inputs):
        return self.linear(inputs.cuda()).cpu()
