import torch
import torchdiffeq
import warnings


def _check_compatability_per_tensor_base(control_gradient, z0):
    if control_gradient.shape[:-1] != z0.shape[:-1]:
        raise ValueError("X.derivative did not return a tensor with the same number of batch dimensions as z0. "
                         "X.derivative returned shape {} (meaning {} batch dimensions), whilst z0 has shape {} "
                         "(meaning {} batch dimensions)."
                         "".format(tuple(control_gradient.shape), tuple(control_gradient.shape[:-1]), tuple(z0.shape),
                                   tuple(z0.shape[:-1])))


def _check_compatability_per_tensor_forward(control_gradient, system, z0):
    _check_compatability_per_tensor_base(control_gradient, z0)
    if system.shape[:-2] != z0.shape[:-1]:
        raise ValueError("func did not return a tensor with the same number of batch dimensions as z0. func returned "
                         "shape {} (meaning {} batch dimensions), whilst z0 has shape {} (meaning {} batch"
                         " dimensions)."
                         "".format(tuple(system.shape), tuple(system.shape[:-2]), tuple(z0.shape),
                                   tuple(z0.shape[:-1])))
    if system.size(-2) != z0.size(-1):
        raise ValueError("func did not return a tensor with the same number of hidden channels as z0. func returned "
                         "shape {} (meaning {} channels), whilst z0 has shape {} (meaning {} channels)."
                         "".format(tuple(system.shape), system.size(-2), tuple(z0.shape), z0.size(-1)))
    if system.size(-1) != control_gradient.size(-1):
        raise ValueError("func did not return a tensor with the same number of input channels as X.derivative "
                         "returned. func returned shape {} (meaning {} channels), whilst X.derivative returned shape "
                         "{} (meaning {} channels)."
                         "".format(tuple(system.shape), system.size(-1), tuple(control_gradient.shape),
                                   control_gradient.size(-1)))


def _check_compatability_per_tensor_prod(control_gradient, vector_field, z0):
    _check_compatability_per_tensor_base(control_gradient, z0)
    if vector_field.shape != z0.shape:
        raise ValueError("func.prod did not return a tensor with the same shape as z0. func.prod returned shape {} "
                         "whilst z0 has shape {}."
                         "".format(tuple(vector_field.shape), tuple(z0.shape)))


def _check_compatability(X, func, z0, t):
    if not hasattr(X, 'derivative'):
        raise ValueError("X must have a 'derivative' vector_field_type.")
    control_gradient = X.derivative(t[0].detach())
    if hasattr(func, 'prod'):
        is_prod = True
        vector_field = func.prod(t[0], z0, control_gradient)
    else:
        is_prod = False
        system = func(t[0], z0)

    if isinstance(z0, torch.Tensor):
        is_tensor = True
        if not isinstance(control_gradient, torch.Tensor):
            raise ValueError("z0 is a tensor and so X.derivative must return a tensor as well.")
        if is_prod:
            if not isinstance(vector_field, torch.Tensor):
                raise ValueError("z0 is a tensor and so func.prod must return a tensor as well.")
            _check_compatability_per_tensor_prod(control_gradient, vector_field, z0)
        else:
            if not isinstance(system, torch.Tensor):
                raise ValueError("z0 is a tensor and so func must return a tensor as well.")
            _check_compatability_per_tensor_forward(control_gradient, system, z0)

    elif isinstance(z0, (tuple, list)):
        is_tensor = False
        if not isinstance(control_gradient, (tuple, list)):
            raise ValueError("z0 is a tuple/list and so X.derivative must return a tuple/list as well.")
        if len(z0) != len(control_gradient):
            raise ValueError("z0 and X.derivative(t) must be tuples of the same length.")
        if is_prod:
            if not isinstance(vector_field, (tuple, list)):
                raise ValueError("z0 is a tuple/list and so func.prod must return a tuple/list as well.")
            if len(z0) != len(vector_field):
                raise ValueError("z0 and func.prod(t, z, dXdt) must be tuples of the same length.")
            for control_gradient_, vector_Field_, z0_ in zip(control_gradient, vector_field, z0):
                if not isinstance(control_gradient_, torch.Tensor):
                    raise ValueError("X.derivative must return a tensor or tuple of tensors.")
                if not isinstance(vector_Field_, torch.Tensor):
                    raise ValueError("func.prod must return a tensor or tuple/list of tensors.")
                _check_compatability_per_tensor_prod(control_gradient_, vector_Field_, z0_)
        else:
            if not isinstance(system, (tuple, list)):
                raise ValueError("z0 is a tuple/list and so func must return a tuple/list as well.")
            if len(z0) != len(system):
                raise ValueError("z0 and func(t, z) must be tuples of the same length.")
            for control_gradient_, system_, z0_ in zip(control_gradient, system, z0):
                if not isinstance(control_gradient_, torch.Tensor):
                    raise ValueError("X.derivative must return a tensor or tuple of tensors.")
                if not isinstance(system_, torch.Tensor):
                    raise ValueError("func must return a tensor or tuple/list of tensors.")
                _check_compatability_per_tensor_forward(control_gradient_, system_, z0_)

    else:
        raise ValueError("z0 must either a tensor or a tuple/list of tensors.")

    return is_tensor, is_prod


class _VectorField(torch.nn.Module):
    def __init__(self, X, func, is_tensor, is_prod, method):
        super(_VectorField, self).__init__()

        self.X = X
        self.func = func
        self.is_tensor = is_tensor
        self.is_prod = is_prod
        self.method = method

    def forward(self, t, z):
        # control_gradient is of shape (..., input_channels)
        control_gradient = self.X.derivative(t)

        if self.is_prod:
            if self.method != 'matmul':
                raise NotImplementedError("only matmul implemented for is_prod")
            # out is of shape (..., hidden_channels)
            out = self.func.prod(t, z, control_gradient)
        else:
            # vector_field isof shape (..., hidden_channels, input_channels)
            if self.method in ['evaluate', 'derivative']:
                get_func_data = getattr(self.X, self.method)
                func_inputs = torch.cat([z, get_func_data(t)], -1)
                print(func_inputs)
                out = self.func(t, func_inputs)
            else:
                vector_field = self.func(t, z)
                if self.is_tensor:
                    # out is of shape (..., hidden_channels)
                    # (The squeezing is necessary to make the matrix-multiply properly batch in all cases)
                    out = (vector_field @ control_gradient.unsqueeze(-1)).squeeze(-1)
                else:
                    out = tuple((vector_field_ @ control_gradient_.unsqueeze(-1)).squeeze(-1)
                                for vector_field_, control_gradient_ in zip(vector_field, control_gradient))

        return out


def cdeint(X, func, z0, t, adjoint=True, vector_field_type='matmul', **kwargs):
    r"""Solves a system of controlled differential equations.

    Solves the controlled problem:
    ```
    z_t = z_{t_0} + \int_{t_0}^t f(s, z_s) dX_s
    ```
    where z is a tensor of any shape, and X is some controlling signal.

    Arguments:
        X: The control. This should be a instance of `torch.nn.Module`, with a `derivative` method. For example
            `torchcde.NaturalCubicSpline`. This represents a continuous path derived from the data. The
            derivative at a point will be computed via `X.derivative(t)`, where t is a scalar tensor. The returned
            tensor should have shape (..., input_channels), where '...' is some number of batch dimensions and
            input_channels is the number of channels in the input path.
        func: Should be a callable describing the vector field f(t, z). If using `adjoint=True` (the default), then
            should be an instance of `torch.nn.Module`, to collect the parameters for the adjoint pass. Will be called
            with a scalar tensor t and a tensor z of shape (..., hidden_channels), and should return a tensor of shape
            (..., hidden_channels, input_channels), where hidden_channels and input_channels are integers defined by the
            `hidden_shape` and `X` arguments as above. The '...' corresponds to some number of batch dimensions. If it
            has a method `prod` then that will be called to calculate the matrix-vector product f(t, z) dX_t/dt, via
            `func.prod(t, z, dXdt)`.
        z0: The initial state of the solution. It should have shape (..., hidden_channels), where '...' is some number
            of batch dimensions.
        t: a one dimensional tensor describing the times to range of times to integrate over and output the results at.
            The initial time will be t[0] and the final time will be t[-1].
        adjoint: A boolean; whether to use the adjoint method to backpropagate. Defaults to True.
        vector_field_type: A string from ('matmul', 'evaluate', 'derivative') that determines how the data interacts with the
            vector field. 'matmul' is the classic approach, 'evaluate' and 'derivative' concatenate the hidden state
            with the data and put everything into the vector field (like an rnn) with 'evaluate' inputting the raw data
            and 'derivative' inputting the derivatives.
        **kwargs: Any additional kwargs to pass to the odeint solver of torchdiffeq (the most common are `rtol`, `atol`,
            `method`, `options`).

    Returns:
        The value of each z_{t_i} of the solution to the CDE z_t = z_{t_0} + \int_0^t f(s, z_s)dX_s, where t_i = t[i].
        This will be a tensor of shape (..., len(t), hidden_channels).

    Raises:
        ValueError for malformed inputs.

    Note:
        Supports tupled input, i.e. z0 can be a tuple of tensors, and X.derivative and func can return tuples of tensors
        of the same length.

    Warnings:
        Note that the returned tensor puts the sequence dimension second-to-last, rather than first like in
        `torchdiffeq.odeint`.
    """
    if vector_field_type not in ['matmul', 'evaluate', 'derivative']:
        raise ValueError("vector_field_type string not recognised")

    # Reduce the default values for the tolerances because CDEs are difficult to solve with the default high tolerances.
    if 'atol' not in kwargs:
        kwargs['atol'] = 1e-6
    if 'rtol' not in kwargs:
        kwargs['rtol'] = 1e-4

    is_tensor, is_prod = _check_compatability(X, func, z0, t)

    if adjoint:
        try:
            _adjoint_params = (id(param) for param in kwargs['adjoint_params'])
        except KeyError:
            _adjoint_params = ()

        for buffer in X.buffers():
            # Compare based on id to avoid PyTorch not playing well with using `in` on tensors.
            if buffer.requires_grad and id(buffer) not in _adjoint_params:
                warnings.warn("One of the inputs to the control path X requires gradients but is not listed in "
                              "`options['adjoint_params']`. This is probably a mistake: it will not receive a gradient "
                              "when using the adjoint vector_field_type. Either have the input not require gradients (if that "
                              "was unintended), or include it (and every other parameter needing gradients) in "
                              "`adjoint_params`. For example:\n"
                              "```\n"
                              "coeffs = ...\n"
                              "func = ...\n"
                              "X = NaturalCubicSpline(coeffs)\n"
                              "adjoint_params = tuple(func.parameters()) + (coeffs,)\n"
                              "cdeint(X=X, func=func, ..., adjoint_params=adjoint_params)\n"
                              "```")

    vector_field = _VectorField(X=X, func=func, is_tensor=is_tensor, is_prod=is_prod, method=vector_field_type)
    odeint = torchdiffeq.odeint_adjoint if adjoint else torchdiffeq.odeint
    out = odeint(func=vector_field, y0=z0, t=t, **kwargs)

    if is_tensor:
        batch_dims = range(1, len(out.shape) - 1)
        out = out.permute(*batch_dims, 0, -1)
    else:
        out_ = []
        for outi in out:
            batch_dims = range(1, len(outi.shape) - 1)
            outi = outi.permute(*batch_dims, 0, -1)
            out_.append(outi)
        out = tuple(out_)

    return out
