import warnings
from scipy.linalg import toeplitz
import scipy.sparse.linalg
import numpy as np
import numba
import cooler
from ._numutils import (
    iterative_correction_symmetric as _iterative_correction_symmetric,
    observed_over_expected as _observed_over_expected,
    fake_cis,
    logbins,
    MatVec,
)


def get_diag(arr, i=0):
    '''Get the i-th diagonal of a matrix.
    This solution was borrowed from
    http://stackoverflow.com/questions/9958577/changing-the-values-of-the-diagonal-of-a-matrix-in-numpy
    '''
    return arr.ravel()[
        max(i,-arr.shape[1]*i)
        :max(0,(arr.shape[1]-i))*arr.shape[1]
        :arr.shape[1]+1]


def set_diag(arr, x, i=0, copy=False):
    '''Rewrite in place the i-th diagonal of a matrix with a value or an array
    of values.
    This solution was borrowed from
    http://stackoverflow.com/questions/9958577/changing-the-values-of-the-diagonal-of-a-matrix-in-numpy'''
    if copy:
        arr = arr.copy()
    arr.flat[
        max(i,-arr.shape[1]*i)
        :max(0,(arr.shape[1]-i))*arr.shape[1]
        :arr.shape[1]+1
        ] = x
    return arr


def fill_diagonal(arr, values, k=0, wrap=False, copy=True):
    """
    Based on numpy.fill_diagonal, but allows for kth diagonals as well.
    Supports 2D arrays, square or rectangular. Returns a copy by default.

    Parameters
    ----------
    arr : 2-D array
        Array whose diagonal is to be filled.
    values : scalar or 1-D vector of correct length
        Values to be written on the diagonal.
    k : int, optional
        Which diagonal to write to. Default is 0.
        Main diagonal is 0; upper diagonals are positive and
        lower diagonals are negative.
    wrap : bool, optional
        For tall matrices, the diagonal is "wrapped" after N columns.
        Default is False.
    copy : bool, optional
        Return a copy. Diagonal is written in-place if false. 
        Default is True.

    Returns
    -------
    Array with diagonal filled.

    """
    if copy:
        arr = arr.copy()
    else:
        arr = np.asarray(arr)
    start = k
    step = arr.shape[1] + 1
    # This is needed so a tall matrix doesn't have the diagonal wrap around.
    if wrap:
        end = None
    else:
        end = start + arr.shape[1] * arr.shape[1]
    arr.flat[start:end:step] = values
    return arr


def fill_na(arr, value=0, copy=True):
    '''Replaces np.nan entries in an array with the provided value.

    Parameters
    ----------

    arr : np.array

    value : float

    copy : bool, optional
        If True, creates a copy of x, otherwise replaces values in-place. 
        By default, True.

    '''
    if copy:
        arr = arr.copy()
    arr[np.isnan(arr)] = value
    return arr


def fill_inf(arr, pos_value=0, neg_value=0, copy=True):
    '''Replaces positive and negative infinity entries in an array 
       with the provided values.

    Parameters
    ----------
    
    arr : np.array

    pos_value : float
        Fill value for np.inf

    neg_value : float
        Fill value for -np.inf

    copy : bool, optional
        If True, creates a copy of x, otherwise replaces values in-place. 
        By default, True.

    '''
    if copy:
        arr = arr.copy()
    arr[np.isposinf(arr)] = pos_value
    arr[np.isneginf(arr)] = neg_value
    return arr


def fill_nainf(arr, value=0, copy=True):
    '''Replaces np.nan and np.inf entries in an array with the provided value.

    Parameters
    ----------

    arr : np.array

    value : float

    copy : bool, optional
        If True, creates a copy of x, otherwise replaces values in-place. 
        By default, True.

    .. note:: differs from np.nan_to_num in that it replaces np.inf with the same
    number as np.nan.
    '''
    if copy:
        arr = arr.copy()
    arr[~np.isfinite(arr)] = value
    return arr


def slice_sorted(arr, lo, hi):
    '''Get the subset of a sorted array with values >=lo and <hi.
    A faster version of arr[(arr>=lo) & (arr<hi)]
    '''
    return arr[np.searchsorted(arr, lo)
               :np.searchsorted(arr, hi)]

def MAD(arr, axis=None, has_nans=False):
    '''Calculate the Median Absolute Deviation from the median.
    
    Parameters
    ----------
    
    arr : np.ndarray
        Input data.
    
    axis : int
        The axis along which to calculate MAD.
    
    has_nans : bool 
        If True, use the slower NaN-aware method to calculate medians.
    '''
    
    if has_nans:
        return np.nanmedian(np.abs(arr - np.nanmedian(arr, axis)), axis)
    else:
        return np.median(np.abs(arr - np.median(arr, axis)), axis)


def COMED(xs, ys, has_nans=False):
    '''Calculate the comedian - the robust median-based counterpart of 
    Pearson's r.

    comedian = med((xs-median(xs))*(ys-median(ys))) / MAD(xs) / MAD(ys)
    
    Parameters
    ----------

    has_nans : bool
        if True, mask (x,y) pairs with at least one NaN

    .. note:: Citations: "On MAD and comedians" by Michael Falk (1997),
    "Robust Estimation of the Correlation Coefficient: An Attempt of Survey"
    by Georgy Shevlyakov and Pavel Smirnov (2011)
    '''

    if has_nans:
        mask = np.isfinite(xs) & np.isfinite(ys)
        xs = xs[mask]
        ys = ys[mask]

    med_x = np.median(xs)
    med_y = np.median(ys)
    comedian = np.median((xs-med_x) * (ys-med_y)) / MAD(xs) / MAD(ys)

    return comedian

    
def normalize_score(arr, norm='z', axis=None, has_nans=True):
    '''Normalize an array by subtracting the first moment and 
    dividing the residual by the second.
    
    Parameters
    ----------
    
    arr : np.ndarray
        Input data.
        
    norm : str
        The type of normalization.
        'z' - report z-scores, 
        norm_arr = (arr - mean(arr)) / std(arr)
        
        'mad' - report deviations from the median in units of MAD 
        (Median Absolute Deviation from the median),
        norm_arr = (arr - median(arr)) / MAD(arr)
        
        'madz' - report robust z-scores, i.e. estimate the mean as 
        the median and the standard error as MAD / 0.67499,
        norm_arr = (arr - median(arr)) / MAD(arr) * 0.67499
        
    axis : int
        The axis along which to calculate the normalization parameters.
    
    has_nans : bool 
        If True, use slower NaN-aware methods to calculate the 
        normalization parameters.
        
    '''
    
    norm_arr = np.copy(arr)
    norm = norm.lower()
    
    if norm == 'z':
        if has_nans:
            norm_arr -= np.nanmean(norm_arr, axis=axis)
            norm_arr /= np.nanstd(norm_arr, axis=axis)
        else:
            norm_arr -= np.mean(norm_arr, axis=axis)
            norm_arr /= np.std(norm_arr, axis=axis)

    elif norm == 'mad' or norm == 'madz':
        if has_nans:
            norm_arr -= np.nanmedian(norm_arr, axis=axis)
        else:
            norm_arr -= np.median(norm_arr, axis=axis)
        norm_arr /= MAD(norm_arr, axis=axis, has_nans=has_nans)
        if norm == 'madz':
            norm_arr *= 0.67449
    else:
        raise ValueError('Unknown norm type: {}'.format(norm))
        
    
    return norm_arr
            

def stochastic_sd(arr, n=10000, seed=0):
    '''Estimate the standard deviation of an array by considering only the 
    subset of its elements.
    
    Parameters
    ----------
    n : int
        The number of elements to consider. If the array contains fewer elements,
        use all.

    seed : int
        The seed for the random number generator.
    '''
    arr = np.asarray(arr)
    if arr.size < n: 
        return np.sqrt(arr.var())
    else:
        return np.sqrt(
            np.random.RandomState(seed).choice(arr.flat, n, replace=True).var())


def is_symmetric(mat):
    """
    Check if a matrix is symmetric.
    """

    maxDiff = np.abs(mat - mat.T).max()
    return maxDiff < stochastic_sd(mat) * 1e-7 + 1e-5


def get_eig(mat, n=3, mask_zero_rows=False, subtract_mean=False, divide_by_mean=False):
    """Perform an eigenvector decomposition.

    Parameters
    ----------

    mat : np.ndarray
        A square matrix, must not contain nans, infs or zero rows.

    n : int
        The number of eigenvectors to return.

    mask_zero_rows : bool
        If True, mask empty rows/columns before eigenvector decomposition.
        Works only with symmetric matrices.

    subtract_mean : bool
        If True, subtract the mean from the matrix.

    divide_by_mean : bool
        If True, divide the matrix by its mean.

    Returns
    -------

    eigvecs : np.ndarray
        An array of eigenvectors (in rows), sorted by a decreasing absolute 
        eigenvalue.

    eigvecs : np.ndarray
        An array of sorted eigenvalues.

    """
    symmetric = is_symmetric(mat)
    if (symmetric 
        and np.sum(np.sum(np.abs(mat), axis=0) == 0) > 0 
        and not mask_zero_rows
        ):
        warnings.warn(
            "The matrix contains empty rows/columns and is symmetric. "
            "Mask the empty rows with remove_zeros=True")

    if mask_zero_rows:
        if not is_symmetric(mat):
            raise ValueError('The input matrix must be symmetric!')

        mask = np.sum(np.abs(mat), axis=0) != 0
        mat_collapsed = mat[mask, :][:, mask]
        eigvecs_collapsed, eigvals = get_eig(
            mat_collapsed, 
            n=n, 
            mask_zero_rows=False, 
            subtract_mean=subtract_mean, 
            divide_by_mean=divide_by_mean)
        n_rows = mat.shape[0]
        eigvecs = np.full((n, n_rows), np.nan)
        for i in range(n):
            eigvecs[i][mask] = eigvecs_collapsed[i]

        return eigvecs, eigvals
    else:
        mat = mat.astype(np.float, copy=True) # make a copy, ensure float
        mean = np.mean(mat)

        if subtract_mean: 
            mat -= mean
        if divide_by_mean:
            mat /= mean
        
        if symmetric:
            eigvals, eigvecs = scipy.sparse.linalg.eigsh(mat, n)
        else:
            eigvals, eigvecs = scipy.sparse.linalg.eigs(mat, n)
        order = np.argsort(-np.abs(eigvals))
        eigvals = eigvals[order]
        eigvecs = eigvecs.T[order]

        return eigvecs, eigvals 


def logbins(lo, hi, ratio=0, N=0, prepend_zero=False):
    """Make bins with edges evenly spaced in log-space.                          
                                                                                 
    Parameters                                                                   
    ----------                                                                   
    lo, hi : int                                                                 
        The span of the bins.                                                    
    ratio : float                                                                
        The target ratio between the upper and the lower edge of each bin.       
        Either ratio or N must be specified.                                     
    N : int                                                                      
        The target number of bins. The resulting number of bins is not guaranteed.
        Either ratio or N must be specified.                                     
                                                                                 
    """                                                                          
    lo = int(lo)                                                                 
    hi = int(hi)                                                                 
    if ratio != 0:                                                               
        if N != 0:                                                               
            raise ValueError("Please specify N or ratio")                        
        N = np.log(hi / lo) / np.log(ratio)                                      
    elif N == 0:                                                                 
        raise ValueError("Please specify N or ratio")                            
    data10 = np.logspace(np.log10(lo), np.log10(hi), N)                          
    data10 = np.array(np.rint(data10), dtype=int)                                
    data10 = np.sort(np.unique(data10))
    assert data10[0] == lo
    assert data10[-1] == hi                                           
    if prepend_zero:
        data10 = np.r_[0, data10]
    return data10                                                         


@numba.jit
def observed_over_expected(
        matrix, 
        mask=np.empty(shape=(0), dtype=np.bool),
        dist_bin_edge_ratio=1.03):
    '''
    Normalize the contact matrix for distance-dependent contact decay.

    The diagonals of the matrix, corresponding to contacts between loci pairs 
    with a fixed distance, are grouped into exponentially growing bins of 
    distances; the diagonals from each bin are normalized by their average value.

    Parameters
    ----------

    matrix : np.ndarray
        A 2D symmetric matrix of contact frequencies.
    mask : np.ndarray
        A 1D or 2D mask of valid data. 
        If 1D, it is interpreted as a mask of "good" bins.
        If 2D, it is interpreted as a mask of "good" pixels.
    dist_bin_edge_ratio : float
        The ratio of the largest and the shortest distance in each distance bin.

    Returns
    -------
    OE : np.ndarray
        The diagonal-normalized matrix of contact frequencies.
    dist_bins : np.ndarray
        The edges of the distance bins used to calculate average 
        distance-dependent contact frequency.
    sum_pixels : np.ndarray
        The sum of contact frequencies in each distance bin.
    n_pixels : np.ndarray
        The total number of valid pixels in each distance bin.

    '''
                                                                                 
    N = matrix.shape[0]                                                 
    mask2d = np.empty(shape=(0,0), dtype=np.bool)
    if (mask.ndim == 1):
        if (mask.size > 0):
            mask2d = mask[:,None] * mask[None, :]
    elif mask.ndim == 2:
        mask2d = mask
    else:
        raise ValueError('The mask must be either 1D or 2D.')
                                                                                 
    data = np.array(matrix, dtype = np.double, order = "C")
    
    has_mask = mask2d.size>0
    dist_bins = np.r_[0, np.array(logbins(1, N, dist_bin_edge_ratio))]
    n_pixels_arr = np.zeros_like(dist_bins[1:])
    sum_pixels_arr = np.zeros_like(dist_bins[1:], dtype='float64')

    bin_idx, n_pixels, sum_pixels = 0, 0, 0

    for bin_idx, lo, hi in zip(range(len(dist_bins)-1), 
                               dist_bins[:-1], 
                               dist_bins[1:]):
        sum_pixels = 0                                                                   
        n_pixels = 0                                                                
        for offset in range(lo, hi):                                             
            for j in range(0, N-offset):                                         
                if not has_mask or mask2d[offset+j, j]:
                    sum_pixels += data[offset+j, j]
                    n_pixels += 1

        n_pixels_arr[bin_idx] = n_pixels
        sum_pixels_arr[bin_idx] = sum_pixels

        if n_pixels == 0:
            continue
        mean_pixel = sum_pixels / n_pixels                                       
        if mean_pixel == 0:                                                          
            continue

        for offset in range(lo, hi):                                         
            for j in range(0, N-offset):
                if not has_mask or mask2d[offset+j, j]:

                    data[offset + j, j] /= mean_pixel                        
                    if offset > 0:                                           
                        data[j, offset+j] /= mean_pixel                      

    return data, dist_bins, sum_pixels_arr, n_pixels_arr


@numba.jit #(nopython=True)
def iterative_correction_symmetric( 
    x, max_iter=1000, ignore_diags = 0, tol=1e-5, verbose=False):
    """The main method for correcting DS and SS read data.                       
    By default does iterative correction, but can perform an M-time correction   
                                                                                 
    Parameters                                                                   
    ----------                                                                   
                                                                                 
    x : np.ndarray                                                               
        A symmetric matrix to correct.                                           
    max_iter : int                                                               
        The maximal number of iterations to take.                                
    ignore_diags : int                                                           
        The number of diagonals to ignore during iterative correction.           
    tol : float                                                            
        If less or equal to zero, will perform max_iter iterations.              
                                                                                 
    """                                                                          
    N = len(x)                                                          
                                                                                 
    _x = x.copy()
    if ignore_diags>0:
        for d in range(0, ignore_diags):
#            set_diag(_x, 0, d) # explicit cycles are easier to jit
             for j in range(0, N-d):
                 _x[j, j+d] = 0
                 _x[j+d, j] = 0
    totalBias = np.ones(N, np.double)     
                                                                                 
    converged = False                                                  
             
    iternum = 0
    mask = np.sum(_x, axis=1)==0
    for iternum in range(max_iter):                                              
        s = np.sum(_x, axis = 1)                                                
                                                                                 
        mask = (s == 0)                                                         
                                                                                           
        s = s / np.mean(s[~mask])                                              
        s[mask] = 1                                                             
        s -= 1                                                                   
        s *= 0.8                                                                 
        s += 1                                                                   
        totalBias *= s                                                           

        
        #_x = _x / s[:, None]  / s[None,:]
        # an explicit cycle is 2x faster here
        for i in range(N):
            for j in range(N):
                _x[i,j] /= s[i] * s[j]

        crit = np.var(s) #np.abs(s - 1).max()
        if verbose:
            print(crit)
            
        if (tol > 0) and (crit < tol):
            converged=True                                                       
            break                                                                
                                                                                 
    corr = totalBias[~mask].mean()  #mean correction factor                      
    _x = _x * corr * corr #renormalizing everything                               
    totalBias /= corr                                                            
    report = {'converged':converged, 'iternum':iternum}                          
                                                                                 
    return _x, totalBias, report


class LazyToeplitz(cooler.core._IndexingMixin):
    """
    A Toeplitz matrix can be represented with one row and one column.
    This lazy toeplitz object supports slice querying to construct dense 
    matrices on the fly.
    
    """
    def __init__(self, c, r=None):
        if r is None:
            r = c
        elif c[0] != r[0]:
            raise ValueError('First element of `c` and `r` should match')
        self._c = c
        self._r = r
        
    @property
    def shape(self):
        return (len(self._c), len(self._r))
    
    def __getitem__(self, key):
        slc0, slc1 = self._unpack_index(key)
        i0, i1 = self._process_slice(slc0, self.shape[0])
        j0, j1 = self._process_slice(slc1, self.shape[1])
        C, R = self._c, self._r
        
        # symmetric query
        if (i0 == j0) and (i1 == j1):
            c = C[0:(i1-i0)]
            r = R[0:(j1-j0)]
        
        # asymmetric query
        else:
            transpose = False
            # tril
            if j0 < i0 or (i0 == j0 and i1 < j1):
                # tranpose the matrix, query, 
                # then transpose the result
                i0, i1, j0, j1 = j0, j1, i0, i1
                C, R = R, C
                transpose = True
               
            c = np.r_[
                R[(j0-i0) : max(0, j0-i1) : -1], 
                C[0 : max(0, i1-j0)]
            ]
            r = R[(j0-i0):(j1-i0)]
            
            if transpose:
                c, r = r, c
        
        return toeplitz(c, r)


def get_kernel(w, p, ktype):
    """
    Return typical kernels given size parameteres w, p,and kernel type.
    
    Parameters
    ----------
    w : int
        Outer kernel size (actually half of it).
    p : int
        Inner kernel size (half of it).
    ktype : str
        Name of the kernel type, could be one of the following: 'donut', 
        'vertical', 'horizontal', 'lowleft', 'upright'.
        
    Returns
    -------
    kernel : ndarray
        A square matrix of int type filled with 1 and 0, according to the 
        kernel type.

    """
    width = 2*w+1
    kernel = np.ones((width,width),dtype=np.int)
    # mesh grid:
    y,x = np.ogrid[-w:w+1, -w:w+1]

    if ktype == 'donut':
        # mask inner pXp square:
        mask = ((((-p)<=x)&(x<=p))&
                (((-p)<=y)&(y<=p)) )
        # mask vertical and horizontal
        # lines of width 1 pixel:
        mask += (x==0)|(y==0)
        # they are all 0:
        kernel[mask] = 0
    elif ktype == 'vertical':
        # mask outside of vertical line
        # of width 3:
        mask = (((-1>x)|(x>1))&((y>=-w)))
        # mask inner pXp square:
        mask += (((-p<=x)&(x<=p))&
                ((-p<=y)&(y<=p)) )
        # kernel masked:
        kernel[mask] = 0
    elif ktype == 'horizontal':
        # mask outside of horizontal line
        # of width 3:
        mask = (((-1>y)|(y>1))&((x>=-w)))
        # mask inner pXp square:
        mask += (((-p<=x)&(x<=p))&
                ((-p<=y)&(y<=p)) )
        # kernel masked:
        kernel[mask] = 0
    # ACHTUNG!!! UPRIGHT AND LOWLEFT ARE SWITCHED ...
    # IT SEEMS FOR UNKNOWN REASON THAT IT SHOULD
    # BE THAT WAY ...
    # OR IT'S A MISTAKE IN hIccups AS WELL ...
    elif ktype == 'upright':
        # mask inner pXp square:
        mask = (((x>=-p))&
                ((y<=p)) )
        mask += (x>=0)
        mask += (y<=0)
        # kernel masked:
        kernel[mask] = 0
    elif ktype == 'lowleft':
        # mask inner pXp square:
        mask = (((x>=-p))&
                ((y<=p)) )
        mask += (x>=0)
        mask += (y<=0)
        # reflect that mask to
        # make it upper-right:
        mask = mask[::-1,::-1]
        # kernel masked:
        kernel[mask] = 0
    else:
        raise ValueError("Kernel-type {} has not been implemented yet".format(ktype))
    return kernel


def coarsen(reduction, x, axes, trim_excess=False):
    """
    Coarsen an array by applying reduction to fixed size neighborhoods.
    Adapted from `dask.array.coarsen` to work on regular numpy arrays.

    Parameters
    ----------
    reduction : function
        Function like np.sum, np.mean, etc...
    x : np.ndarray
        Array to be coarsened
    axes : dict
        Mapping of axis to coarsening factor
    trim_excess : bool, optional
        Remove excess elements. Default is False.

    Examples
    --------
    Provide dictionary of scale per dimension
    
    >>> x = np.array([1, 2, 3, 4, 5, 6])
    >>> coarsen(np.sum, x, {0: 2})
    array([ 3,  7, 11])
    
    >>> coarsen(np.max, x, {0: 3})
    array([3, 6])

    >>> x = np.arange(24).reshape((4, 6))
    >>> x
    array([[ 0,  1,  2,  3,  4,  5],
           [ 6,  7,  8,  9, 10, 11],
           [12, 13, 14, 15, 16, 17],
           [18, 19, 20, 21, 22, 23]])

    >>> coarsen(np.min, x, {0: 2, 1: 3})
    array([[ 0,  3],
           [12, 15]])

    See also
    --------
    dask.array.coarsen  

    """
    # Insert singleton dimensions if they don't exist already
    for i in range(x.ndim):
        if i not in axes:
            axes[i] = 1

    if trim_excess:
        ind = tuple(slice(0, -(d % axes[i])) 
                        if d % axes[i] else slice(None, None) 
                    for i, d in enumerate(x.shape))
        x = x[ind]

    # (10, 10) -> (5, 2, 5, 2)
    newdims = [(x.shape[i] // axes[i], axes[i]) for i in range(x.ndim)]
    newshape = tuple(np.concatenate(newdims))
    reduction_axes = tuple(range(1, x.ndim * 2, 2))
    return reduction(x.reshape(newshape), axis=reduction_axes)
