import functools as ft
import pytest
import torch
import torchcontroldiffeq


def test_computed_parameter():
    class TestPath(torch.nn.Module):
        def __init__(self):
            super(TestPath, self).__init__()
            x = torch.rand(3, requires_grad=True)
            torchcontroldiffeq.register_computed_parameter(self, 'variable', x.clone())
            torchcontroldiffeq.register_computed_parameter(self, 'variable2', self.variable.clone())

    test_path = TestPath()
    grad = torch.autograd.grad(test_path.variable2.sum(), test_path.variable, allow_unused=True)
    grad2 = torch.autograd.grad(test_path.variable.sum(), test_path.variable2, allow_unused=True)
    # Despite one having been created from the other in __init__, they should have had views taken of them afterwards
    # to ensure that they're not in each other's computation graphs
    assert grad[0] is None
    assert grad2[0] is None

    if torch.cuda.is_available():
        test_path = test_path.to('cuda')
        assert test_path.variable.device.type == 'cuda'
        assert test_path.variable2.device.type == 'cuda'


class _Func(torch.nn.Module):
    def __init__(self, input_size, hidden_size):
        super(_Func, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.variable = torch.nn.Parameter(torch.rand(1, 1, input_size))

    def forward(self, t, z):
        assert z.shape == (1, self.hidden_size)
        out = z.sigmoid().unsqueeze(-1) + self.variable
        assert out.shape == (1, self.hidden_size, self.input_size)
        return out


# Test that gradients can propagate through the controlling path at all
def test_grad_paths():
    for method in ('rk4', 'dopri5'):
        for adjoint in (True, False):
            t = torch.linspace(0, 9, 10, requires_grad=True)
            path = torch.rand(1, 10, 3, requires_grad=True)
            coeffs = torchcontroldiffeq.natural_cubic_spline_coeffs(t, path)
            cubic_spline = torchcontroldiffeq.NaturalCubicSpline(t, coeffs)
            z0 = torch.rand(1, 3, requires_grad=True)
            func = _Func(input_size=3, hidden_size=3)
            t_ = torch.tensor([0., 9.], requires_grad=True)

            z = torchcontroldiffeq.cdeint(X=cubic_spline, func=func, z0=z0, t=t_, adjoint=adjoint, method=method)
            assert z.shape == (1, 2, 3)
            assert t.grad is None
            assert path.grad is None
            assert z0.grad is None
            assert func.variable.grad is None
            assert t_.grad is None
            z[:, 1].sum().backward()
            assert isinstance(t.grad, torch.Tensor)
            assert isinstance(path.grad, torch.Tensor)
            assert isinstance(z0.grad, torch.Tensor)
            assert isinstance(func.variable.grad, torch.Tensor)
            assert isinstance(t_.grad, torch.Tensor)


# Test that gradients flow back through multiple CDEs stacked on top of one another, and that they do so correctly
# without going through earlier parts of the graph multiple times.
def test_stacked_paths():
    class Record(torch.autograd.Function):
        @staticmethod
        def forward(ctx, name, x):
            ctx.name = name
            return x

        @staticmethod
        def backward(ctx, x):
            if hasattr(ctx, 'been_here_before'):
                pytest.fail(ctx.name)
            ctx.been_here_before = True
            return None, x

    ReparameterisedLinearInterpolation = ft.partial(torchcontroldiffeq.LinearInterpolation, reparameterise=True)
    coeff_paths = [(torchcontroldiffeq.linear_interpolation_coeffs, torchcontroldiffeq.LinearInterpolation),
                   (torchcontroldiffeq.linear_interpolation_coeffs, ReparameterisedLinearInterpolation),
                   (torchcontroldiffeq.natural_cubic_spline_coeffs, torchcontroldiffeq.NaturalCubicSpline)]
    for method in ('rk4', 'dopri5'):
        for adjoint in (False, True):
            for first_coeffs, First in coeff_paths:
                for second_coeffs, Second in coeff_paths:
                    for third_coeffs, Third in coeff_paths:
                        first_t = torch.linspace(0, 999, 1000)
                        first_path = torch.rand(1, 1000, 4, requires_grad=True)
                        first_coeff = first_coeffs(first_t, first_path)
                        first_X = First(first_t, first_coeff)
                        first_func = _Func(input_size=4, hidden_size=4)

                        second_t = torch.linspace(0, 999, 100)
                        second_path = torchcontroldiffeq.cdeint(X=first_X, func=first_func, z0=torch.rand(1, 4),
                                                                t=second_t, adjoint=adjoint, method=method)
                        second_path = Record.apply('second', second_path)
                        second_coeff = second_coeffs(second_t, second_path)
                        second_X = Second(second_t, second_coeff)
                        second_func = _Func(input_size=4, hidden_size=4)

                        third_t = torch.linspace(0, 999, 10)
                        third_path = torchcontroldiffeq.cdeint(X=second_X, func=second_func, z0=torch.rand(1, 4),
                                                               t=third_t, adjoint=adjoint, method=method)
                        third_path = Record.apply('third', third_path)
                        third_coeff = third_coeffs(third_t, third_path)
                        third_X = Third(third_t, third_coeff)
                        third_func = _Func(input_size=4, hidden_size=5)

                        fourth_t = torch.tensor([0, 999.])
                        fourth_path = torchcontroldiffeq.cdeint(X=third_X, func=third_func, z0=torch.rand(1, 5),
                                                                t=fourth_t, adjoint=adjoint, method=method)
                        fourth_path = Record.apply('fourth', fourth_path)
                        assert first_func.variable.grad is None
                        assert second_func.variable.grad is None
                        assert third_func.variable.grad is None
                        assert first_path.grad is None
                        fourth_path[:, -1].sum().backward()
                        assert isinstance(first_func.variable.grad, torch.Tensor)
                        assert isinstance(second_func.variable.grad, torch.Tensor)
                        assert isinstance(third_func.variable.grad, torch.Tensor)
                        assert isinstance(first_path.grad, torch.Tensor)


# Tests that the trick in which we use detaches in the backward pass if possible, does in fact work
def test_detach_trick():
    t = torch.linspace(0, 9, 10)
    path = torch.rand(1, 10, 3)
    func = _Func(input_size=3, hidden_size=3)

    def interp_():
        coeffs = torchcontroldiffeq.natural_cubic_spline_coeffs(t, path)
        yield torchcontroldiffeq.NaturalCubicSpline(t, coeffs)
        coeffs = torchcontroldiffeq.linear_interpolation_coeffs(t, path)
        yield torchcontroldiffeq.LinearInterpolation(t, coeffs, reparameterise=True)

    for interp in interp_():
        for adjoint in (True, False):
            variable_grads = []
            z0 = torch.rand(1, 3)
            for t_grad in (True, False):
                t_ = torch.tensor([0., 9.], requires_grad=t_grad)
                # Don't test dopri5. We will get different results then, because the t variable will force smaller step
                # sizes and thus slightly different results.
                z = torchcontroldiffeq.cdeint(X=interp, z0=z0, func=func, t=t_, adjoint=adjoint, method='rk4',
                                              options=dict(step_size=0.5))
                z[:, -1].sum().backward()
                variable_grads.append(func.variable.grad.clone())
                func.variable.grad.zero_()

            for elem in variable_grads[1:]:
                assert (elem == variable_grads[0]).all()