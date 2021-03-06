#!/usr/bin/env python

from typing import Tuple, Union

import numpy as np
import scipy.stats
import scipy.special

import numba as nb


# caching some values for efficiency
taylor_range = np.arange(1, 60)
taylor_factors = scipy.special.factorial(taylor_range) / np.sqrt(taylor_range)


@nb.vectorize([nb.float64(nb.float64)], target="parallel")
def taylor_expand(x):
    return np.exp(-x) * (x ** taylor_range / taylor_factors).sum()


@nb.vectorize([nb.float64(nb.float64)], target="parallel")
def taylor_around_mean(x):
    return np.sqrt(x) - x ** (-0.5) / 8 + x ** (-1.5) / 16 - 5 * x ** (-2.5) / 128


def expected_sqrt(mean_expression: np.ndarray, cutoff: float = 34.94) -> np.ndarray:
    """Return expected square root of a poisson distribution. Uses Taylor series
     centered at 0 or mean, as appropriate.

    :param mean_expression: Array of expected mean expression values
    :param cutoff: point for switching between approximations (default is ~optimal)
    :return: Array of expected sqrt mean expression values
    """
    above_cutoff = mean_expression >= cutoff

    truncated_taylor = taylor_expand(np.minimum(mean_expression, cutoff))
    truncated_taylor[above_cutoff] = taylor_around_mean(mean_expression[above_cutoff])

    return truncated_taylor


def convert_expectations(
    exp_sqrt: np.ndarray,
    a: Union[float, np.ndarray],
    b: Union[float, np.ndarray] = None,
) -> np.ndarray:
    """Takes expected sqrt expression calculated for one scaling factor and converts
    to the corresponding levels at a second scaling factor

    :param exp_sqrt: Expected sqrt values calculated at ``scale``
    :param a: Scaling factor(s) of the input data
    :param b: Scale for the output. Set to ``1 - a`` by default
    :return: A scaled array of expected sqrt expression
    """
    if b is None:
        b = 1.0 - a

    exp_sqrt = np.maximum(exp_sqrt, 0)
    max_val = np.max(exp_sqrt ** 2) / np.min(a)

    vs = 2 ** np.arange(0, np.ceil(np.log2(max_val + 1)) + 1) - 1
    p_range = np.hstack(
        [np.arange(v, vs[i + 1], 2 ** (i + 1) * 0.01) for i, v in enumerate(vs[:-1])]
    )

    xp = expected_sqrt(p_range * a)
    fp = expected_sqrt(p_range * b)

    if isinstance(a, np.ndarray) or isinstance(b, np.ndarray):
        xp = np.broadcast_to(xp, (exp_sqrt.shape[0], p_range.shape[0]))
        fp = np.broadcast_to(fp, (exp_sqrt.shape[0], p_range.shape[0]))

        interps = np.empty_like(exp_sqrt)
        for i in range(exp_sqrt.shape[0]):
            interps[i, :] = np.interp(exp_sqrt[i, :], xp[i, :], fp[i, :])

        return interps
    else:
        return np.interp(exp_sqrt, xp, fp)


def poisson_fit(umis: np.ndarray) -> np.ndarray:
    """Takes an array of UMI counts and calculates per-gene deviation from a poisson
    distribution representing even expression across all cells.

    :param umis: Unscaled UMI counts for ``n_cells * n_genes``
    :return: An array of p-values of size ``n_genes``
    """
    n_cells = umis.shape[0]
    pct = (umis > 0).sum(0) / n_cells
    exp = umis.sum(0) / umis.sum()
    numis = umis.sum(1)

    prob_zero = np.exp(-np.dot(exp[:, None], numis[None, :]))
    exp_pct_nz = (1 - prob_zero).mean(1)

    var_pct_nz = (prob_zero * (1 - prob_zero)).mean(1) / n_cells
    std_pct_nz = np.sqrt(var_pct_nz)

    exp_p = np.ones_like(pct)
    ix = std_pct_nz != 0
    exp_p[ix] = scipy.stats.norm.cdf(pct[ix], loc=exp_pct_nz[ix], scale=std_pct_nz[ix])

    return exp_p


def overlap_correction(
    data_split: float, sample_ratio: Union[float, np.ndarray]
) -> Union[float, np.ndarray]:
    """Computes expected overlap between two independent draws from a cell generated by
    partitioning a sample into two groups, taking into account the finite size of the
    original cell.

    Calculating this factor is recommended if the sample counts are believed to be a
    significant fraction of the total molecules in the original cell.

    :param data_split: Proportion of the sample going into the first group
    :param sample_ratio: Ratio of counts in the sample compared to the original cells
    :return: Adjusted values for data_split, and the overlap correction factor
    """
    if np.all(sample_ratio == 0.0):
        return data_split, 1 - data_split, 0.0

    a = (1 - data_split) / data_split

    p = ((1 + a) - np.sqrt((1 + a) ** 2 - 4 * a * sample_ratio)) / (2 * a)
    q = a * p

    assert np.allclose(p + q - p * q, sample_ratio)

    new_split = p / sample_ratio
    new_split_complement = q / sample_ratio
    overlap_factor = new_split + new_split_complement - 1

    return new_split, new_split_complement, overlap_factor


def split_molecules(
    umis: np.ndarray,
    data_split: float,
    overlap_factor: float = 0.0,
    random_state: np.random.RandomState = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """Splits molecules into two (potentially overlapping) groups.

    :param umis: Array of molecules to split
    :param data_split: Proportion of molecules to assign to the first group
    :param overlap_factor: Overlap correction factor, if desired
    :param random_state: For reproducible sampling
    :return: umis_X and umis_Y, representing ``split`` and ``~(1 - split)`` counts
             sampled from the input array
    """
    if random_state is None:
        random_state = np.random.RandomState()

    umis_X_disjoint = random_state.binomial(umis, data_split - overlap_factor)
    umis_Y_disjoint = random_state.binomial(
        umis - umis_X_disjoint, (1 - data_split) / (1 - data_split + overlap_factor)
    )
    overlap_factor = umis - umis_X_disjoint - umis_Y_disjoint
    umis_X = umis_X_disjoint + overlap_factor
    umis_Y = umis_Y_disjoint + overlap_factor

    return umis_X, umis_Y
