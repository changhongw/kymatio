# Authors: Mathieu Andreux, Joakim Anden, Edouard Oyallon
# Scientific Ancestry: Joakim Anden, Mathieu Andreux, Vincent Lostanlen

import math
import numbers

import torch
import numpy as np

from ...frontend.torch_frontend import ScatteringTorch

from kymatio.scattering1d.core.scattering1d import scattering1d

from kymatio.scattering1d.filter_bank import (calibrate_scattering_filters,
                                              scattering_filter_factory)
from kymatio.scattering1d.utils import compute_border_indices, compute_padding

__all__ = ['Scattering1DTorch']

class Scattering1DTorch(ScatteringTorch):
    """The 1D scattering transform

    The scattering transform computes a cascade of wavelet transforms
    alternated with a complex modulus non-linearity. The scattering transform
    of a 1D signal :math:`x(t)` may be written as

        $S_J x = [S_J^{(0)} x, S_J^{(1)} x, S_J^{(2)} x]$

    where

        $S_J^{(0)} x(t) = x \\star \\phi_J(t)$,

        $S_J^{(1)} x(t, \\lambda) =|x \\star \\psi_\\lambda^{(1)}| \\star \\phi_J$, and

        $S_J^{(2)} x(t, \\lambda, \\mu) = |\\,| x \\star \\psi_\\lambda^{(1)}| \\star \\psi_\\mu^{(2)} | \\star \\phi_J$.

    In the above formulas, :math:`\\star` denotes convolution in time. The
    filters $\\psi_\\lambda^{(1)}(t)$ and $\\psi_\\mu^{(2)}(t)$
    are analytic wavelets with center frequencies $\\lambda$ and
    $\\mu$, while $\\phi_J(t)$ is a real lowpass filter centered
    at the zero frequency.

    The `Scattering1D` class implements the 1D scattering transform for a
    given set of filters whose parameters are specified at initialization.
    While the wavelets are fixed, other parameters may be changed after the
    object is created, such as whether to compute all of :math:`S_J^{(0)} x`,
    $S_J^{(1)} x$, and $S_J^{(2)} x$ or just $S_J^{(0)} x$
    and $S_J^{(1)} x$.

    The scattering transform may be computed on the CPU (the default) or a
    GPU, if available. A `Scattering1D` object may be transferred from one
    to the other using the `cuda()` and `cpu()` methods.

    Given an input Tensor `x` of size `(B, T)`, where `B` is the number of
    signals to transform (the batch size) and `T` is the length of the signal,
    we compute its scattering transform by passing it to the `forward()`
    method.

    Example
    -------
    ::

        # Set the parameters of the scattering transform.
        J = 6
        T = 2**13
        Q = 8

        # Generate a sample signal.
        x = torch.randn(1, 1, T)

        # Define a Scattering1D object.
        S = Scattering1D(J, T, Q)

        # Calculate the scattering transform.
        Sx = S.forward(x)

    Above, the length of the signal is `T = 2**13 = 8192`, while the maximum
    scale of the scattering transform is set to `2**J = 2**6 = 64`. The
    time-frequency resolution of the first-order wavelets
    :math:`\\psi_\\lambda^{(1)}(t)` is set to `Q = 8` wavelets per octave.
    The second-order wavelets :math:`\\psi_\\mu^{(2)}(t)` always have one
    wavelet per octave.

    Parameters
    ----------
    J : int
        The maximum log-scale of the scattering transform. In other words,
        the maximum scale is given by `2**J`.
    T : int
        The length of the input signals.
    Q : int >= 1
        The number of first-order wavelets per octave (second-order wavelets
        are fixed to one wavelet per octave). Defaults to `1`.
    max_order : int, optional
        The maximum order of scattering coefficients to compute. Must be either
        `1` or `2`. Defaults to `2`.
    average : boolean, optional
        Determines whether the output is averaged in time or not. The averaged
        output corresponds to the standard scattering transform, while the
        un-averaged output skips the last convolution by :math:`\\phi_J(t)`.
        This parameter may be modified after object creation.
        Defaults to `True`.
    oversampling : integer >= 0, optional
        Controls the oversampling factor relative to the default as a power
        of two. Since the convolving by wavelets (or lowpass filters) and
        taking the modulus reduces the high-frequency content of the signal,
        we can subsample to save space and improve performance. However, this
        may reduce precision in the calculation. If this is not desirable,
        `oversampling` can be set to a large value to prevent too much
        subsampling. This parameter may be modified after object creation.
        Defaults to `0`.
    vectorize : boolean, optional
        Determines wheter to return a vectorized scattering transform (that
        is, a large array containing the output) or a dictionary (where each
        entry corresponds to a separate scattering coefficient). This parameter
        may be modified after object creation. Defaults to True.

    Attributes
    ----------
    J : int
        The maximum log-scale of the scattering transform. In other words,
        the maximum scale is given by `2**J`.
    shape : int
        The length of the input signals.
    Q : int
        The number of first-order wavelets per octave (second-order wavelets
        are fixed to one wavelet per octave).
    J_pad : int
        The logarithm of the padded length of the signals.
    pad_left : int
        The amount of padding to the left of the signal.
    pad_right : int
        The amount of padding to the right of the signal.
    phi_f : dictionary
        A dictionary containing the lowpass filter at all resolutions. See
        `filter_bank.scattering_filter_factory` for an exact description.
    psi1_f : dictionary
        A dictionary containing all the first-order wavelet filters, each
        represented as a dictionary containing that filter at all
        resolutions. See `filter_bank.scattering_filter_factory` for an exact
        description.
    psi2_f : dictionary
        A dictionary containing all the second-order wavelet filters, each
        represented as a dictionary containing that filter at all
        resolutions. See `filter_bank.scattering_filter_factory` for an exact
        description.
        description
    max_order : int
        The maximum scattering order of the transform.
    average : boolean
        Controls whether the output should be averaged (the standard
        scattering transform) or not (resulting in wavelet modulus
        coefficients). Note that to obtain unaveraged output, the `vectorize`
        flag must be set to `False`.
    oversampling : int
        The number of powers of two to oversample the output compared to the
        default subsampling rate determined from the filters.
    vectorize : boolean
        Controls whether the output should be vectorized into a single Tensor
        or collected into a dictionary. For more details, see the
        documentation for `forward()`.
    """
    def __init__(self, J, shape, Q=1, max_order=2, average=True,
                 oversampling=0, vectorize=True, backend=None):
        super(Scattering1DTorch, self).__init__()
        # Store the parameters
        self.J = J
        self.shape = shape
        self.Q = Q

        self.max_order = max_order
        self.average = average
        self.oversampling = oversampling
        self.vectorize = vectorize
        self.backend = backend

        # Build internal values
        self.build()

    def build(self):
        """Set up padding and filters

        Certain internal data, such as the amount of padding and the wavelet
        filters to be used in the scattering transform, need to be computed
        from the parameters given during construction. This function is called
        automatically during object creation and no subsequent calls are
        therefore needed.
        """

        # Set these default values for now. In the future, we'll want some
        # flexibility for these, but for now, let's keep them fixed.
        if not self.backend:
            from ..backend import torch_backend as backend
            self.backend = backend
        elif self.backend.name[0:5] != 'torch':
            raise RuntimeError('This backend is not supported.')

        self.r_psi = math.sqrt(0.5)
        self.sigma0 = 0.1
        self.alpha = 5.
        self.P_max = 5
        self.eps = 1e-7
        self.criterion_amplitude = 1e-3
        self.normalize = 'l1'

        # check the shape
        if isinstance(self.shape, numbers.Integral):
            self.T = self.shape
        elif isinstance(self.shape, tuple):
            self.T = self.shape[0]
            if len(self.shape) > 1:
                raise ValueError("If shape is specified as a tuple, it must "
                                 "have exactly one element")
        else:
            raise ValueError("shape must be an integer or a 1-tuple")

        # Compute the minimum support to pad (ideally)
        min_to_pad = compute_minimum_support_to_pad(
            self.T, self.J, self.Q, r_psi=self.r_psi, sigma0=self.sigma0,
            alpha=self.alpha, P_max=self.P_max, eps=self.eps,
            criterion_amplitude=self.criterion_amplitude,
            normalize=self.normalize)
        # to avoid padding more than T - 1 on the left and on the right,
        # since otherwise torch sends nans
        J_max_support = int(np.floor(np.log2(3 * self.T - 2)))
        self.J_pad = min(int(np.ceil(np.log2(self.T + 2 * min_to_pad))),
                         J_max_support)
        # compute the padding quantities:
        self.pad_left, self.pad_right = compute_padding(self.J_pad, self.T)
        # compute start and end indices
        self.ind_start, self.ind_end = compute_border_indices(
            self.J, self.pad_left, self.pad_left + self.T)
        self.create_and_register_filters()

    def create_and_register_filters(self):
        """ This function run the filterbank function that
            will create the filters as numpy array, and then, it
            saves those arrays as module's buffers."""

        # Create the filters
        phi_f, psi1_f, psi2_f, _ = scattering_filter_factory(
            self.J_pad, self.J, self.Q, normalize=self.normalize,
            criterion_amplitude=self.criterion_amplitude,
            r_psi=self.r_psi, sigma0=self.sigma0, alpha=self.alpha,
            P_max=self.P_max, eps=self.eps)

        n = 0
        # prepare for pytorch
        for k in phi_f.keys():
            if type(k) != str:
                # view(-1, 1).repeat(1, 2) because real numbers!
                phi_f[k] = torch.from_numpy(
                    phi_f[k]).view(-1, 1).repeat(1, 2)
                self.register_buffer('tensor' + str(n), phi_f[k])
                n += 1
        for psi_f in psi1_f:
            for sub_k in psi_f.keys():
                if type(sub_k) != str:
                    # view(-1, 1).repeat(1, 2) because real numbers!
                    psi_f[sub_k] = torch.from_numpy(
                        psi_f[sub_k]).view(-1, 1).repeat(1, 2)
                    self.register_buffer('tensor' + str(n), psi_f[sub_k])
                    n += 1
        for psi_f in psi2_f:
            for sub_k in psi_f.keys():
                if type(sub_k) != str:
                    # view(-1, 1).repeat(1, 2) because real numbers!
                    psi_f[sub_k] = torch.from_numpy(
                        psi_f[sub_k]).view(-1, 1).repeat(1, 2)
                    self.register_buffer('tensor' + str(n), psi_f[sub_k])
                    n += 1

        self.psi1_f = psi1_f
        self.psi2_f = psi2_f
        self.phi_f = phi_f

    def meta(self):
        """Get meta information on the transform

        Calls the static method `compute_meta_scattering()` with the
        parameters of the transform object.

        Returns
        ------
        meta : dictionary
            See the documentation for `compute_meta_scattering()`.
        """
        return Scattering1DTorch.compute_meta_scattering(
            self.J, self.Q, max_order=self.max_order)

    def output_size(self, detail=False):
        """Get size of the scattering transform

        Calls the static method `precompute_size_scattering()` with the
        parameters of the transform object.

        Parameters
        ----------
        detail : boolean, optional
            Specifies whether to provide a detailed size (number of coefficient
            per order) or an aggregate size (total number of coefficients).

        Returns
        ------
        size : int or tuple
            See the documentation for `precompute_size_scattering()`.
        """

        return Scattering1DTorch.precompute_size_scattering(
            self.J, self.Q, max_order=self.max_order, detail=detail)

    def forward(self, x):
        """Apply the scattering transform

        Given an input Tensor of size `(B, T0)`, where `B` is the batch
        size and `T0` is the length of the individual signals, this function
        computes its scattering transform. If the `vectorize` flag is set to
        `True`, the output is in the form of a Tensor or size `(B, C, T1)`,
        where `T1` is the signal length after subsampling to the scale `2**J`
        (with the appropriate oversampling factor to reduce aliasing), and
        `C` is the number of scattering coefficients.  If `vectorize` is set
        `False`, however, the output is a dictionary containing `C` keys, each
        a tuple whose length corresponds to the scattering order and whose
        elements are the sequence of filter indices used.

        Furthermore, if the `average` flag is set to `False`, these outputs
        are not averaged, but are simply the wavelet modulus coefficients of
        the filters.

        Parameters
        ----------
        x : tensor
            An input Tensor of size `(B, T0)`.

        Returns
        -------
        S : tensor or dictionary
            If the `vectorize` flag is `True`, the output is a Tensor
            containing the scattering coefficients, while if `vectorize`
            is `False`, it is a dictionary indexed by tuples of filter indices.
        """
        # basic checking, should be improved
        if len(x.shape) < 1:
            raise ValueError(
                'Input tensor x should have at least one axis, got {}'.format(
                    len(x.shape)))

        batch_shape = x.shape[:-1]
        signal_shape = x.shape[-1:]

        x = x.reshape((-1, 1) + signal_shape)

        # get the arguments before calling the scattering
        # treat the arguments
        if self.vectorize:
            if not(self.average):
                raise ValueError(
                    'Options average=False and vectorize=True are ' +
                    'mutually incompatible. Please set vectorize to False.')
            size_scattering = self.precompute_size_scattering(
                self.J, self.Q, max_order=self.max_order, detail=True)
        else:
            size_scattering = 0

        n = 0
        buffer_dict = dict(self.named_buffers())
        for k in self.phi_f.keys():
            if type(k) != str:
                # view(-1, 1).repeat(1, 2) because real numbers!
                self.phi_f[k] = buffer_dict['tensor' + str(n)]
                n += 1
        for psi_f in self.psi1_f:
            for sub_k in psi_f.keys():
                if type(sub_k) != str:
                    # view(-1, 1).repeat(1, 2) because real numbers!
                    self.psi_f[sub_k] = buffer_dict['tensor' + str(n)]
                    n += 1
        for psi_f in self.psi2_f:
            for sub_k in psi_f.keys():
                if type(sub_k) != str:
                    # view(-1, 1).repeat(1, 2) because real numbers!
                    psi_f[sub_k] = buffer_dict['tensor' + str(n)]
                    n += 1

        S = scattering1d(x, pad, unpad, self.backend, self.J, self.psi1_f, self.psi2_f, self.phi_f,\
                         max_order=self.max_order, average=self.average,
                       pad_left=self.pad_left, pad_right=self.pad_right,
                       ind_start=self.ind_start, ind_end=self.ind_end,
                       oversampling=self.oversampling,
                       vectorize=self.vectorize,
                       size_scattering=size_scattering)

        if self.vectorize:
            scattering_shape = S.shape[-2:]
            S = S.reshape(batch_shape + scattering_shape)
        else:
            for k, v in S.items():
                scattering_shape = v.shape[-2:]
                S[k] = v.reshape(batch_shape + scattering_shape)

        return S

    @staticmethod
    def compute_meta_scattering(J, Q, max_order=2):
        """Get metadata on the transform.

        This information specifies the content of each scattering coefficient,
        which order, which frequencies, which filters were used, and so on.

        Parameters
        ----------
        J : int
            The maximum log-scale of the scattering transform.
            In other words, the maximum scale is given by `2**J`.
        Q : int >= 1
            The number of first-order wavelets per octave.
            Second-order wavelets are fixed to one wavelet per octave.
        max_order : int, optional
            The maximum order of scattering coefficients to compute.
            Must be either equal to `1` or `2`. Defaults to `2`.

        Returns
        -------
        meta : dictionary
            A dictionary with the following keys:

            - `'order`' : tensor
                A Tensor of length `C`, the total number of scattering
                coefficients, specifying the scattering order.
            - `'xi'` : tensor
                A Tensor of size `(C, max_order)`, specifying the center
                frequency of the filter used at each order (padded with NaNs).
            - `'sigma'` : tensor
                A Tensor of size `(C, max_order)`, specifying the frequency
                bandwidth of the filter used at each order (padded with NaNs).
            - `'j'` : tensor
                A Tensor of size `(C, max_order)`, specifying the dyadic scale
                of the filter used at each order (padded with NaNs).
            - `'n'` : tensor
                A Tensor of size `(C, max_order)`, specifying the indices of
                the filters used at each order (padded with NaNs).
            - `'key'` : list
                The tuples indexing the corresponding scattering coefficient
                in the non-vectorized output.
        """
        sigma_low, xi1s, sigma1s, j1s, xi2s, sigma2s, j2s = \
            calibrate_scattering_filters(J, Q)

        meta = {}

        meta['order'] = [[], [], []]
        meta['xi'] = [[], [], []]
        meta['sigma'] = [[], [], []]
        meta['j'] = [[], [], []]
        meta['n'] = [[], [], []]
        meta['key'] = [[], [], []]

        meta['order'][0].append(0)
        meta['xi'][0].append(())
        meta['sigma'][0].append(())
        meta['j'][0].append(())
        meta['n'][0].append(())
        meta['key'][0].append(())

        for (n1, (xi1, sigma1, j1)) in enumerate(zip(xi1s, sigma1s, j1s)):
            meta['order'][1].append(1)
            meta['xi'][1].append((xi1,))
            meta['sigma'][1].append((sigma1,))
            meta['j'][1].append((j1,))
            meta['n'][1].append((n1,))
            meta['key'][1].append((n1,))

            if max_order < 2:
                continue

            for (n2, (xi2, sigma2, j2)) in enumerate(zip(xi2s, sigma2s, j2s)):
                if j2 > j1:
                    meta['order'][2].append(2)
                    meta['xi'][2].append((xi1, xi2))
                    meta['sigma'][2].append((sigma1, sigma2))
                    meta['j'][2].append((j1, j2))
                    meta['n'][2].append((n1, n2))
                    meta['key'][2].append((n1, n2))

        for field, value in meta.items():
            meta[field] = value[0] + value[1] + value[2]

        pad_fields = ['xi', 'sigma', 'j', 'n']
        pad_len = max_order

        for field in pad_fields:
            meta[field] = [x + (math.nan,) * (pad_len - len(x)) for x in meta[field]]

        array_fields = ['order', 'xi', 'sigma', 'j', 'n']

        for field in array_fields:
            meta[field] = torch.from_numpy(np.array(meta[field]))

        return meta

    @staticmethod
    def precompute_size_scattering(J, Q, max_order=2, detail=False):
        """Get size of the scattering transform

        The number of scattering coefficients depends on the filter
        configuration and so can be calculated using a few of the scattering
        transform parameters.

        Parameters
        ----------
        J : int
            The maximum log-scale of the scattering transform.
            In other words, the maximum scale is given by `2**J`.
        Q : int >= 1
            The number of first-order wavelets per octave.
            Second-order wavelets are fixed to one wavelet per octave.
        max_order : int, optional
            The maximum order of scattering coefficients to compute.
            Must be either equal to `1` or `2`. Defaults to `2`.
        detail : boolean, optional
            Specifies whether to provide a detailed size (number of coefficient
            per order) or an aggregate size (total number of coefficients).

        Returns
        -------
        size : int or tuple
            If `detail` is `False`, returns the number of coefficients as an
            integer. If `True`, returns a tuple of size `max_order` containing
            the number of coefficients in each order.
        """
        sigma_low, xi1, sigma1, j1, xi2, sigma2, j2 = \
            calibrate_scattering_filters(J, Q)

        size_order0 = 1
        size_order1 = len(xi1)
        size_order2 = 0
        for n1 in range(len(xi1)):
            for n2 in range(len(xi2)):
                if j2[n2] > j1[n1]:
                    size_order2 += 1
        if detail:
            if max_order == 2:
                return size_order0, size_order1, size_order2
            else:
                return size_order0, size_order1
        else:
            if max_order == 2:
                return size_order0 + size_order1 + size_order2
            else:
                return size_order0 + size_order1

    def compute_minimum_support_to_pad(T, J, Q, criterion_amplitude=1e-3,
                                       normalize='l1', r_psi=math.sqrt(0.5),
                                       sigma0=1e-1, alpha=5., P_max=5, eps=1e-7):
        """
        Computes the support to pad given the input size and the parameters of the
        scattering transform.

        Parameters
        ----------
        T : int
            temporal size of the input signal
        J : int
            scale of the scattering
        Q : int
            number of wavelets per octave
        normalize : string, optional
            normalization type for the wavelets.
            Only `'l2'` or `'l1'` normalizations are supported.
            Defaults to `'l1'`
        criterion_amplitude: float `>0` and `<1`, optional
            Represents the numerical error which is allowed to be lost after
            convolution and padding.
            The larger criterion_amplitude, the smaller the padding size is.
            Defaults to `1e-3`
        r_psi : float, optional
            Should be `>0` and `<1`. Controls the redundancy of the filters
            (the larger r_psi, the larger the overlap between adjacent
            wavelets).
            Defaults to `sqrt(0.5)`.
        sigma0 : float, optional
            parameter controlling the frequential width of the
            low-pass filter at J_scattering=0; at a an absolute J_scattering,
            it is equal to :math:`\\frac{\\sigma_0}{2^J}`.
            Defaults to `1e-1`.
        alpha : float, optional
            tolerance factor for the aliasing after subsampling.
            The larger the alpha, the more conservative the value of maximal
            subsampling is.
            Defaults to `5`.
        P_max : int, optional
            maximal number of periods to use to make sure that the Fourier
            transform of the filters is periodic.
            `P_max = 5` is more than enough for double precision.
            Defaults to `5`.
        eps : float, optional
            required machine precision for the periodization (single
            floating point is enough for deep learning applications).
            Defaults to `1e-7`.

        Returns
        -------
        min_to_pad: int
            minimal value to pad the signal on one size to avoid any
            boundary error.
        """
        J_tentative = int(np.ceil(np.log2(T)))
        _, _, _, t_max_phi = scattering_filter_factory(
            J_tentative, J, Q, normalize=normalize, to_torch=False,
            max_subsampling=0, criterion_amplitude=criterion_amplitude,
            r_psi=r_psi, sigma0=sigma0, alpha=alpha, P_max=P_max, eps=eps)
        min_to_pad = 3 * t_max_phi
        return min_to_pad



def _apply_psi(Psi, fn):
    """
    Casts the filters contained in Psi to the required type, by following
    the dictionary structure.

    Parameters
    ----------
    Psi : dictionary
        dictionary of dictionary of filters, should be psi1_f or psi2_f
    _type : torch type
        required type to cast the filters to. Should be a torch.FloatTensor

    Returns
    -------
    Nothing - function modifies the input
    """
    for filt in Psi:
        for k in filt.keys():
            if torch.is_tensor(filt[k]):
                filt[k] = fn(filt[k])


def _apply_phi(Phi, fn):
    """
    Casts the filters contained in Phi to the required type, by following
    the dictionary structure.

    Parameters
    ----------
    Psi : dictionary
        dictionary of filters, should be phi_f
    _type : torch type
        required type to cast the filters to. Should be a torch.FloatTensor

    Returns
    -------
    Nothing - function modifies the input
    """
    for k in Phi.keys():
        if torch.is_tensor(Phi[k]):
            Phi[k] = fn(Phi[k])