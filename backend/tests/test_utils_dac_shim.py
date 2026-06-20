"""
Unit tests for ``backend.utils.dac_shim`` — the minimal shim that exposes
``Snake1d`` (and the underlying ``snake`` activation) without dragging in the
full ``descript-audio-codec``/``descript-audiotools`` dependency stack.

Behavior under test:

* ``snake(x, alpha)`` computes ``x + (alpha + 1e-9).reciprocal() * sin(alpha*x)**2``
  while preserving the input tensor shape.
* ``Snake1d`` is an ``nn.Module`` that holds a learnable ``alpha`` parameter of
  shape ``(1, channels, 1)`` initialized to ones and applies ``snake`` in
  ``forward``.
* ``install_dac_shim`` is a no-op when ``dac`` is already importable and
  otherwise registers a synthetic ``dac`` package (with ``dac.nn.layers`` and
  ``dac.model.dac`` exposing ``Snake1d``) in ``sys.modules`` so downstream
  ``from dac.nn.layers import Snake1d`` style imports succeed.
"""

import importlib
import math
import sys
from pathlib import Path

import pytest
import torch

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import dac_shim  # noqa: E402


# ── snake() ─────────────────────────────────────────────────────────


def test_snake_matches_reference_formula():
    """snake(x, alpha) == x + (alpha + 1e-9).reciprocal() * sin(alpha*x)**2."""
    torch.manual_seed(0)
    x = torch.randn(2, 3, 4)
    alpha = torch.full((1, 3, 1), 0.5)

    expected = x + (alpha + 1e-9).reciprocal() * torch.sin(alpha * x).pow(2)
    actual = dac_shim.snake(x, alpha)

    assert torch.allclose(actual, expected, atol=1e-6)


def test_snake_preserves_input_shape():
    """The activation must round-trip the input shape (including >3 dims)."""
    x = torch.randn(2, 3, 4, 5)
    alpha = torch.ones(1, 3, 1)

    result = dac_shim.snake(x, alpha)

    assert result.shape == x.shape


def test_snake_zero_input_is_zero():
    """sin(alpha*0) == 0, so snake(0, alpha) == 0 for any finite alpha."""
    x = torch.zeros(1, 4, 8)
    alpha = torch.full((1, 4, 1), 2.5)

    result = dac_shim.snake(x, alpha)

    assert torch.allclose(result, torch.zeros_like(x))


def test_snake_scalar_alpha_known_value():
    """Hand-computed value for x=π/2, alpha=1 → π/2 + 1/(1+1e-9) * sin(π/2)**2 ≈ π/2 + 1."""
    x = torch.tensor([[[math.pi / 2]]])
    alpha = torch.tensor([[[1.0]]])

    result = dac_shim.snake(x, alpha)

    expected = math.pi / 2 + 1.0 / (1.0 + 1e-9) * math.sin(math.pi / 2) ** 2
    assert result.item() == pytest.approx(expected, rel=1e-6)


def test_snake_is_differentiable_w_r_t_alpha():
    """alpha must receive a gradient — this is how the activation is trained."""
    x = torch.randn(1, 2, 3)
    alpha = torch.full((1, 2, 1), 0.7, requires_grad=True)

    out = dac_shim.snake(x, alpha)
    out.sum().backward()

    assert alpha.grad is not None
    assert alpha.grad.shape == alpha.shape
    assert torch.isfinite(alpha.grad).all()


# ── Snake1d module ─────────────────────────────────────────────────


def test_snake1d_alpha_parameter_shape_and_init():
    """alpha is a learnable parameter of shape (1, channels, 1) initialized to ones."""
    module = dac_shim.Snake1d(channels=8)

    assert isinstance(module.alpha, torch.nn.Parameter)
    assert module.alpha.requires_grad is True
    assert tuple(module.alpha.shape) == (1, 8, 1)
    assert torch.equal(module.alpha.detach(), torch.ones(1, 8, 1))


def test_snake1d_forward_matches_functional_snake():
    """Snake1d.forward(x) must equal snake(x, self.alpha)."""
    torch.manual_seed(1)
    module = dac_shim.Snake1d(channels=4)
    # Perturb alpha away from the constant initialization so the test is not
    # accidentally satisfied by a no-op.
    with torch.no_grad():
        module.alpha.copy_(torch.linspace(0.1, 1.0, 4).view(1, 4, 1))
    x = torch.randn(2, 4, 6)

    out = module(x)
    expected = dac_shim.snake(x, module.alpha)

    assert out.shape == x.shape
    assert torch.allclose(out, expected, atol=1e-6)


def test_snake1d_is_nn_module():
    """Required so optimizers see the alpha parameter via .parameters()."""
    module = dac_shim.Snake1d(channels=3)

    params = list(module.parameters())
    assert len(params) == 1
    assert params[0] is module.alpha


# ── install_dac_shim() ──────────────────────────────────────────────


@pytest.fixture
def clean_dac_modules():
    """Snapshot sys.modules entries for dac* and restore them after the test."""
    keys = ["dac", "dac.nn", "dac.nn.layers", "dac.model", "dac.model.dac"]
    saved = {k: sys.modules.get(k) for k in keys}
    # Remove them so install_dac_shim() takes the "no real package" branch.
    for k in keys:
        sys.modules.pop(k, None)
    # Also drop any cached importlib finders for "dac"
    importlib.invalidate_caches()
    try:
        yield
    finally:
        for k in keys:
            if saved[k] is not None:
                sys.modules[k] = saved[k]
            else:
                sys.modules.pop(k, None)
        importlib.invalidate_caches()


def test_install_dac_shim_registers_expected_modules(clean_dac_modules):
    dac_shim.install_dac_shim()

    for name in ["dac", "dac.nn", "dac.nn.layers", "dac.model", "dac.model.dac"]:
        assert name in sys.modules, f"{name} should be registered"


def test_install_dac_shim_exposes_snake1d_via_dac_nn_layers(clean_dac_modules):
    dac_shim.install_dac_shim()

    from dac.nn.layers import Snake1d as ShimmedSnake1d  # type: ignore

    assert ShimmedSnake1d is dac_shim.Snake1d


def test_install_dac_shim_exposes_snake1d_via_dac_model_dac(clean_dac_modules):
    dac_shim.install_dac_shim()

    from dac.model.dac import Snake1d as ShimmedSnake1d  # type: ignore

    assert ShimmedSnake1d is dac_shim.Snake1d


def test_install_dac_shim_wires_submodules_as_attributes(clean_dac_modules):
    dac_shim.install_dac_shim()

    dac_pkg = sys.modules["dac"]
    assert dac_pkg.nn is sys.modules["dac.nn"]
    assert dac_pkg.model is sys.modules["dac.model"]
    assert dac_pkg.nn.layers is sys.modules["dac.nn.layers"]
    assert dac_pkg.model.dac is sys.modules["dac.model.dac"]


def test_install_dac_shim_exposes_functional_snake(clean_dac_modules):
    dac_shim.install_dac_shim()

    from dac.nn.layers import snake as shimmed_snake  # type: ignore

    assert shimmed_snake is dac_shim.snake


def test_install_dac_shim_is_noop_when_real_dac_already_present(clean_dac_modules):
    """If a `dac` package is already importable, the shim must not overwrite it."""
    import types

    real_dac = types.ModuleType("dac")
    real_dac.__path__ = []
    real_dac.sentinel = "real"  # marker so we can detect overwrite
    sys.modules["dac"] = real_dac

    dac_shim.install_dac_shim()

    assert sys.modules["dac"] is real_dac
    assert sys.modules["dac"].sentinel == "real"
    # Submodules must not have been planted underneath the real package.
    assert "dac.nn.layers" not in sys.modules
    assert "dac.model.dac" not in sys.modules


def test_install_dac_shim_marks_packages_with_path(clean_dac_modules):
    """Package-like modules need __path__ so submodule imports resolve."""
    dac_shim.install_dac_shim()

    assert hasattr(sys.modules["dac"], "__path__")
    assert hasattr(sys.modules["dac.nn"], "__path__")
    assert hasattr(sys.modules["dac.model"], "__path__")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
