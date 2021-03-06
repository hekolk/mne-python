# Authors: Chris Holdgraf <choldgraf@gmail.com>
#
# License: BSD (3-clause)
import warnings
import os.path as op

from nose.tools import assert_raises, assert_true, assert_equal
import numpy as np

from numpy.testing import assert_array_equal, assert_allclose

from mne import io, pick_types
from mne.fixes import einsum
from mne.utils import requires_version, run_tests_if_main, check_version
from mne.decoding import ReceptiveField, TimeDelayingRidge
from mne.decoding.receptive_field import (_delay_time_series, _SCORERS,
                                          _times_to_delays, _delays_to_slice)
from mne.decoding.time_delaying_ridge import (_compute_reg_neighbors,
                                              _compute_corrs)


data_dir = op.join(op.dirname(__file__), '..', '..', 'io', 'tests', 'data')
raw_fname = op.join(data_dir, 'test_raw.fif')
event_name = op.join(data_dir, 'test-eve.fif')

rng = np.random.RandomState(1337)

tmin, tmax = -0.1, 0.5
event_id = dict(aud_l=1, vis_l=3)

warnings.simplefilter('always')

# Loading raw data
raw = io.read_raw_fif(raw_fname, preload=True)
picks = pick_types(raw.info, meg=True, stim=False, ecg=False,
                   eog=False, exclude='bads')
picks = picks[:2]


def test_compute_reg_neighbors():
    """Test fast calculation of laplacian regularizer."""
    for reg_type in (
            ('ridge', 'ridge'),
            ('ridge', 'laplacian'),
            ('laplacian', 'ridge'),
            ('laplacian', 'laplacian')):
        for n_ch_x, n_delays in (
                (1, 1), (1, 2), (2, 1), (1, 3), (3, 1), (1, 4), (4, 1),
                (2, 2), (2, 3), (3, 2), (3, 3),
                (2, 4), (4, 2), (3, 4), (4, 3), (4, 4),
                (5, 4), (4, 5), (5, 5),
                (20, 9), (9, 20)):
            for normed in (True, False):
                reg_direct = _compute_reg_neighbors(
                    n_ch_x, n_delays, reg_type, 'direct', normed=normed)
                reg_csgraph = _compute_reg_neighbors(
                    n_ch_x, n_delays, reg_type, 'csgraph', normed=normed)
                assert_allclose(
                    reg_direct, reg_csgraph, atol=1e-7,
                    err_msg='%s: %s' % (reg_type, (n_ch_x, n_delays)))


@requires_version('sklearn', '0.17')
def test_rank_deficiency():
    """Test signals that are rank deficient."""
    # See GH#4253
    from sklearn.linear_model import Ridge
    N = 256
    fs = 1.
    tmin, tmax = -50, 100
    reg = 0.1
    rng = np.random.RandomState(0)
    eeg = rng.randn(N, 1)
    eeg *= 100
    eeg = np.fft.rfft(eeg, axis=0)
    eeg[N // 4:] = 0  # rank-deficient lowpass
    eeg = np.fft.irfft(eeg, axis=0)
    win = np.hanning(N // 8)
    win /= win.mean()
    y = np.apply_along_axis(np.convolve, 0, eeg, win, mode='same')
    y += rng.randn(*y.shape) * 100

    for est in (Ridge(reg), reg):
        rf = ReceptiveField(tmin, tmax, fs, estimator=est, patterns=True)
        rf.fit(eeg, y)
        pred = rf.predict(eeg)
        assert_equal(y.shape, pred.shape)
        corr = np.corrcoef(y.ravel(), pred.ravel())[0, 1]
        assert_true(corr > 0.995, msg=corr)


def test_time_delay():
    """Test that time-delaying w/ times and samples works properly."""
    # Explicit delays + sfreq
    X = np.random.RandomState(0).randn(1000, 2)
    assert (X == 0).sum() == 0  # need this for later
    test_tlims = [
        ((1, 2), 1),
        ((1, 1), 1),
        ((0, 2), 1),
        ((0, 1), 1),
        ((0, 0), 1),
        ((-1, 2), 1),
        ((-1, 1), 1),
        ((-1, 0), 1),
        ((-1, -1), 1),
        ((-2, 2), 1),
        ((-2, 1), 1),
        ((-2, 0), 1),
        ((-2, -1), 1),
        ((-2, -1), 1),
        ((0, .2), 10),
        ((-.1, .1), 10)]
    for (tmin, tmax), isfreq in test_tlims:
        # sfreq must be int/float
        assert_raises(ValueError, _delay_time_series, X, tmin, tmax,
                      sfreq=[1])
        # Delays must be int/float
        assert_raises(ValueError, _delay_time_series, X,
                      np.complex(tmin), tmax, 1)
        # Make sure swapaxes works
        start, stop = int(round(tmin * isfreq)), int(round(tmax * isfreq)) + 1
        n_delays = stop - start
        X_delayed = _delay_time_series(X, tmin, tmax, isfreq)
        assert_equal(X_delayed.shape, (1000, 2, n_delays))
        # Make sure delay slice is correct
        delays = _times_to_delays(tmin, tmax, isfreq)
        assert_array_equal(delays, np.arange(start, stop))
        keep = _delays_to_slice(delays)
        expected = np.where((X_delayed != 0).all(-1).all(-1))[0]
        got = np.arange(len(X_delayed))[keep]
        assert_array_equal(got, expected)
        assert_true(X_delayed[keep].shape[-1] > 0)
        assert_true((X_delayed[keep] == 0).sum() == 0)

        del_zero = int(round(-tmin * isfreq))
        for ii in range(-2, 3):
            idx = del_zero + ii
            err_msg = '[%s,%s] (%s): %s %s' % (tmin, tmax, isfreq, ii, idx)
            if 0 <= idx < X_delayed.shape[-1]:
                if ii == 0:
                    assert_array_equal(X_delayed[:, :, idx], X,
                                       err_msg=err_msg)
                elif ii < 0:  # negative delay
                    assert_array_equal(X_delayed[:ii, :, idx], X[-ii:, :],
                                       err_msg=err_msg)
                    assert_array_equal(X_delayed[ii:, :, idx], 0.)
                else:
                    assert_array_equal(X_delayed[ii:, :, idx], X[:-ii, :],
                                       err_msg=err_msg)
                    assert_array_equal(X_delayed[:ii, :, idx], 0.)


@requires_version('sklearn', '0.17')
def test_receptive_field():
    """Test model prep and fitting."""
    from sklearn.linear_model import Ridge
    # Make sure estimator pulling works
    mod = Ridge()

    # Test the receptive field model
    # Define parameters for the model and simulate inputs + weights
    tmin, tmax = -10., 0
    n_feats = 3
    X = rng.randn(10000, n_feats)
    w = rng.randn(int((tmax - tmin) + 1) * n_feats)

    # Delay inputs and cut off first 4 values since they'll be cut in the fit
    X_del = np.concatenate(
        _delay_time_series(X, tmin, tmax, 1.).transpose(2, 0, 1), axis=1)
    y = np.dot(X_del, w)

    # Fit the model and test values
    feature_names = ['feature_%i' % ii for ii in [0, 1, 2]]
    rf = ReceptiveField(tmin, tmax, 1, feature_names, estimator=mod,
                        patterns=True)
    rf.fit(X, y)
    assert_array_equal(rf.delays_, np.arange(tmin, tmax + 1))

    y_pred = rf.predict(X)
    assert_allclose(y[rf.valid_samples_], y_pred[rf.valid_samples_], atol=1e-2)
    scores = rf.score(X, y)
    assert_true(scores > .99)
    assert_allclose(rf.coef_.T.ravel(), w, atol=1e-2)
    # Make sure different input shapes work
    rf.fit(X[:, np.newaxis:, ], y[:, np.newaxis])
    rf.fit(X, y[:, np.newaxis])
    assert_raises(ValueError, rf.fit, X[..., np.newaxis], y)
    assert_raises(ValueError, rf.fit, X[:, 0], y)
    assert_raises(ValueError, rf.fit, X[..., np.newaxis],
                  np.tile(y[..., np.newaxis], [2, 1, 1]))
    # stim features must match length of input data
    assert_raises(ValueError, rf.fit, X[:, :1], y)
    # auto-naming features
    rf = ReceptiveField(tmin, tmax, 1, estimator=mod)
    rf.fit(X, y)
    assert_equal(rf.feature_names, ['feature_%s' % ii for ii in [0, 1, 2]])
    # X/y same n timepoints
    assert_raises(ValueError, rf.fit, X, y[:-2])
    # Float becomes ridge
    rf = ReceptiveField(tmin, tmax, 1, ['one', 'two', 'three'],
                        estimator=0, patterns=True)
    str(rf)  # repr works before fit
    rf.fit(X, y)
    assert_true(isinstance(rf.estimator_, TimeDelayingRidge))
    str(rf)  # repr works after fit
    rf = ReceptiveField(tmin, tmax, 1, ['one'], estimator=0, patterns=True)
    rf.fit(X[:, [0]], y)
    str(rf)  # repr with one feature
    # Should only accept estimators or floats
    rf = ReceptiveField(tmin, tmax, 1, estimator='foo', patterns=True)
    assert_raises(ValueError, rf.fit, X, y)
    rf = ReceptiveField(tmin, tmax, 1, estimator=np.array([1, 2, 3]))
    assert_raises(ValueError, rf.fit, X, y)
    # tmin must be <= tmax
    rf = ReceptiveField(5, 4, 1, patterns=True)
    assert_raises(ValueError, rf.fit, X, y)
    # scorers
    for key, val in _SCORERS.items():
        rf = ReceptiveField(tmin, tmax, 1, ['one'],
                            estimator=0, scoring=key, patterns=True)
        rf.fit(X[:, [0]], y)
        y_pred = rf.predict(X[:, [0]]).T.ravel()[:, np.newaxis]
        assert_allclose(val(y[:, np.newaxis], y_pred,
                            multioutput='raw_values'),
                        rf.score(X[:, [0]], y), rtol=1e-2)
    # Need 2D input
    assert_raises(ValueError, _SCORERS['corrcoef'], y.ravel(), y_pred,
                  multioutput='raw_values')
    # Need correct scorers
    rf = ReceptiveField(tmin, tmax, 1., scoring='foo')
    assert_raises(ValueError, rf.fit, X, y)


def test_time_delaying_fast_calc():
    """Test time delaying and fast calculations."""
    X = np.array([[1, 2, 3], [5, 7, 11]]).T
    # all negative
    smin, smax = 1, 2
    X_del = _delay_time_series(X, smin, smax, 1.)
    # (n_times, n_features, n_delays) -> (n_times, n_features * n_delays)
    X_del.shape = (X.shape[0], -1)
    expected = np.array([[0, 1, 2], [0, 0, 1], [0, 5, 7], [0, 0, 5]]).T
    assert_allclose(X_del, expected)
    Xt_X = np.dot(X_del.T, X_del)
    expected = [[5, 2, 19, 10], [2, 1, 7, 5], [19, 7, 74, 35], [10, 5, 35, 25]]
    assert_allclose(Xt_X, expected)
    x_xt = _compute_corrs(X, np.zeros((X.shape[0], 1)), smin, smax + 1)[0]
    assert_allclose(x_xt, expected)
    # all positive
    smin, smax = -2, -1
    X_del = _delay_time_series(X, smin, smax, 1.)
    X_del.shape = (X.shape[0], -1)
    expected = np.array([[3, 0, 0], [2, 3, 0], [11, 0, 0], [7, 11, 0]]).T
    assert_allclose(X_del, expected)
    Xt_X = np.dot(X_del.T, X_del)
    expected = [[9, 6, 33, 21], [6, 13, 22, 47],
                [33, 22, 121, 77], [21, 47, 77, 170]]
    assert_allclose(Xt_X, expected)
    x_xt = _compute_corrs(X, np.zeros((X.shape[0], 1)), smin, smax + 1)[0]
    assert_allclose(x_xt, expected)
    # both sides
    smin, smax = -1, 1
    X_del = _delay_time_series(X, smin, smax, 1.)
    X_del.shape = (X.shape[0], -1)
    expected = np.array([[2, 3, 0], [1, 2, 3], [0, 1, 2],
                         [7, 11, 0], [5, 7, 11], [0, 5, 7]]).T
    assert_allclose(X_del, expected)
    Xt_X = np.dot(X_del.T, X_del)
    expected = [[13, 8, 3, 47, 31, 15],
                [8, 14, 8, 29, 52, 31],
                [3, 8, 5, 11, 29, 19],
                [47, 29, 11, 170, 112, 55],
                [31, 52, 29, 112, 195, 112],
                [15, 31, 19, 55, 112, 74]]
    assert_allclose(Xt_X, expected)
    x_xt = _compute_corrs(X, np.zeros((X.shape[0], 1)), smin, smax + 1)[0]
    assert_allclose(x_xt, expected)

    # slightly harder to get the non-Toeplitz correction correct
    X = np.array([[1, 2, 3, 5]]).T
    smin, smax = 0, 3
    X_del = _delay_time_series(X, smin, smax, 1.)
    X_del.shape = (X.shape[0], -1)
    expected = np.array([[1, 2, 3, 5], [0, 1, 2, 3],
                         [0, 0, 1, 2], [0, 0, 0, 1]]).T
    assert_allclose(X_del, expected)
    Xt_X = np.dot(X_del.T, X_del)
    expected = [[39, 23, 13, 5], [23, 14, 8, 3], [13, 8, 5, 2], [5, 3, 2, 1]]
    assert_allclose(Xt_X, expected)
    x_xt = _compute_corrs(X, np.zeros((X.shape[0], 1)), smin, smax + 1)[0]
    assert_allclose(x_xt, expected)

    # even worse
    X = np.array([[1, 2, 3], [5, 7, 11]]).T
    smin, smax = 0, 2
    X_del = _delay_time_series(X, smin, smax, 1.)
    X_del.shape = (X.shape[0], -1)
    expected = np.array([[1, 2, 3], [0, 1, 2], [0, 0, 1],
                         [5, 7, 11], [0, 5, 7], [0, 0, 5]]).T
    assert_allclose(X_del, expected)
    Xt_X = np.dot(X_del.T, X_del)
    expected = np.array([[14, 8, 3, 52, 31, 15],
                         [8, 5, 2, 29, 19, 10],
                         [3, 2, 1, 11, 7, 5],
                         [52, 29, 11, 195, 112, 55],
                         [31, 19, 7, 112, 74, 35],
                         [15, 10, 5, 55, 35, 25]])
    assert_allclose(Xt_X, expected)
    x_xt = _compute_corrs(X, np.zeros((X.shape[0], 1)), smin, smax + 1)[0]
    assert_allclose(x_xt, expected)

    # And a bunch of random ones for good measure
    rng = np.random.RandomState(0)
    X = rng.randn(25, 3)
    y = np.empty((25, 2))
    vals = (0, -1, 1, -2, 2, -11, 11)
    for smax in vals:
        for smin in vals:
            if smin > smax:
                continue
            for ii in range(X.shape[1]):
                kernel = rng.randn(smax - smin + 1)
                kernel -= np.mean(kernel)
                y[:, ii % y.shape[-1]] = np.convolve(X[:, ii], kernel, 'same')
            x_xt, x_yt, n_ch_x = _compute_corrs(X, y, smin, smax + 1)
            X_del = _delay_time_series(X, smin, smax, 1., fill_mean=False)
            x_yt_true = einsum('tfd,to->ofd', X_del, y)
            x_yt_true = np.reshape(x_yt_true, (x_yt_true.shape[0], -1)).T
            assert_allclose(x_yt, x_yt_true, atol=1e-7, err_msg=(smin, smax))
            X_del.shape = (X.shape[0], -1)
            x_xt_true = np.dot(X_del.T, X_del).T
            assert_allclose(x_xt, x_xt_true, atol=1e-7, err_msg=(smin, smax))


@requires_version('sklearn', '0.17')
def test_receptive_field_1d():
    """Test that the fast solving works like Ridge."""
    from sklearn.linear_model import Ridge
    rng = np.random.RandomState(0)
    x = rng.randn(500, 1)
    for delay in range(-2, 3):
        y = np.zeros(500)
        slims = [(-2, 4)]
        if delay == 0:
            y[:] = x[:, 0]
        elif delay < 0:
            y[:delay] = x[-delay:, 0]
            slims += [(-4, -1)]
        else:
            y[delay:] = x[:-delay, 0]
            slims += [(1, 2)]
        for ndim in (1, 2):
            y.shape = (y.shape[0],) + (1,) * (ndim - 1)
            for slim in slims:
                lap = TimeDelayingRidge(slim[0], slim[1], 1., 0.1, 'laplacian',
                                        fit_intercept=False)
                for estimator in (Ridge(alpha=0.), Ridge(alpha=0.1), 0., 0.1,
                                  lap):
                    for offset in (-100, 0, 100):
                        model = ReceptiveField(slim[0], slim[1], 1.,
                                               estimator=estimator)
                        use_x = x + offset
                        model.fit(use_x, y)
                        if estimator is lap:
                            continue  # these checks are too stringent
                        assert_allclose(model.estimator_.intercept_, -offset,
                                        atol=1e-1)
                        assert_array_equal(model.delays_,
                                           np.arange(slim[0], slim[1] + 1))
                        expected = (model.delays_ == delay).astype(float)
                        expected = expected[np.newaxis]  # features
                        if y.ndim == 2:
                            expected = expected[np.newaxis]  # outputs
                        assert_equal(model.coef_.ndim, ndim + 1)
                        assert_allclose(model.coef_, expected, atol=1e-3)
                        start = model.valid_samples_.start or 0
                        stop = len(use_x) - (model.valid_samples_.stop or 0)
                        assert stop - start >= 495
                        assert_allclose(
                            model.predict(use_x)[model.valid_samples_],
                            y[model.valid_samples_], atol=1e-2)
                        score = np.mean(model.score(use_x, y))
                        assert_true(score > 0.9999, msg=score)


@requires_version('sklearn', '0.17')
def test_receptive_field_nd():
    """Test multidimensional support."""
    from sklearn.linear_model import Ridge
    # multidimensional
    x = rng.randn(1000, 3)
    y = np.zeros((1000, 2))
    slim = [0, 5]
    # This is a weird assignment, but it's just a way to distribute some
    # unique values at various delays, and "expected" explains how they
    # should appear in the resulting RF
    for ii in range(1, 5):
        y[ii:, ii % 2] += (-1) ** ii * ii * x[:-ii, ii % 3]
    y -= np.mean(y, axis=0)
    x -= np.mean(x, axis=0)
    x_off = x + 1e3
    expected = [
        [[0, 0, 0, 0, 0, 0],
         [0, 0, 0, 0, 4, 0],
         [0, 0, 2, 0, 0, 0]],
        [[0, 0, 0, -3, 0, 0],
         [0, -1, 0, 0, 0, 0],
         [0, 0, 0, 0, 0, 0]],
    ]
    tdr = TimeDelayingRidge(slim[0], slim[1], 1., 0.1, 'laplacian')
    for estimator in (Ridge(alpha=0.), 0., 0.01, tdr):
        model = ReceptiveField(slim[0], slim[1], 1.,
                               estimator=estimator)
        model.fit(x, y)
        assert_array_equal(model.delays_,
                           np.arange(slim[0], slim[1] + 1))
        assert_allclose(model.coef_, expected, atol=1e-1)
    tdr = TimeDelayingRidge(slim[0], slim[1], 1., 0.01, reg_type='foo')
    model = ReceptiveField(slim[0], slim[1], 1., estimator=tdr)
    assert_raises(ValueError, model.fit, x, y)
    tdr = TimeDelayingRidge(slim[0], slim[1], 1., 0.01, reg_type=['laplacian'])
    model = ReceptiveField(slim[0], slim[1], 1., estimator=tdr)
    assert_raises(ValueError, model.fit, x, y)

    # Now check the intercept_
    tdr = TimeDelayingRidge(slim[0], slim[1], 1., 0.)
    tdr_no = TimeDelayingRidge(slim[0], slim[1], 1., 0., fit_intercept=False)
    for estimator in (Ridge(alpha=0.), tdr,
                      Ridge(alpha=0., fit_intercept=False), tdr_no):
        # first with no intercept in the data
        model = ReceptiveField(slim[0], slim[1], 1., estimator=estimator)
        model.fit(x, y)
        assert_allclose(model.estimator_.intercept_, 0., atol=1e-7,
                        err_msg=repr(estimator))
        assert_allclose(model.coef_, expected, atol=1e-3,
                        err_msg=repr(estimator))
        y_pred = model.predict(x)
        assert_allclose(y_pred[model.valid_samples_],
                        y[model.valid_samples_],
                        atol=1e-2, err_msg=repr(estimator))
        score = np.mean(model.score(x, y))
        assert score > 0.9999

        # now with an intercept in the data
        model.fit(x_off, y)
        if estimator.fit_intercept:
            val = [-6000, 4000]
            itol = 0.5
            ctol = 5e-4
        else:
            val = itol = 0.
            ctol = 2.
        assert_allclose(model.estimator_.intercept_, val, atol=itol,
                        err_msg=repr(estimator))
        assert_allclose(model.coef_, expected, atol=ctol, rtol=ctol,
                        err_msg=repr(estimator))
        if estimator.fit_intercept:
            ptol = 1e-2
            stol = 0.999999
        else:
            ptol = 10
            stol = 0.6
        y_pred = model.predict(x_off)[model.valid_samples_]
        assert_allclose(y_pred, y[model.valid_samples_],
                        atol=ptol, err_msg=repr(estimator))
        score = np.mean(model.score(x_off, y))
        assert score > stol, estimator
        model = ReceptiveField(slim[0], slim[1], 1., fit_intercept=False)
        model.fit(x_off, y)
        assert_allclose(model.estimator_.intercept_, 0., atol=1e-7)
        score = np.mean(model.score(x_off, y))
        assert_true(score > 0.6, msg=score)


@requires_version('sklearn', '0.17')
def test_inverse_coef():
    """Test inverse coefficients computation."""
    from sklearn.linear_model import Ridge

    rng = np.random.RandomState(0)
    tmin, tmax = 0., 10.
    n_feats, n_targets, n_samples = 64, 2, 10000
    n_delays = int((tmax - tmin) + 1)

    def make_data(n_feats, n_targets, n_samples, tmin, tmax):
        X = rng.randn(n_samples, n_feats)
        w = rng.randn(int((tmax - tmin) + 1) * n_feats, n_targets)
        # Delay inputs
        X_del = np.concatenate(
            _delay_time_series(X, tmin, tmax, 1.).transpose(2, 0, 1), axis=1)
        y = np.dot(X_del, w)
        return X, y

    # Check coefficient dims, for all estimator types
    X, y = make_data(n_feats, n_targets, n_samples, tmin, tmax)
    tdr = TimeDelayingRidge(tmin, tmax, 1., 0.1, 'laplacian')
    for estimator in (0., 0.01, Ridge(alpha=0.), tdr):
        rf = ReceptiveField(tmin, tmax, 1., estimator=estimator,
                            patterns=True)
        rf.fit(X, y)
        inv_rf = ReceptiveField(tmin, tmax, 1., estimator=estimator,
                                patterns=True)
        inv_rf.fit(y, X)

        assert_array_equal(rf.coef_.shape, rf.patterns_.shape,
                           (n_targets, n_feats, n_delays))
        assert_array_equal(inv_rf.coef_.shape, inv_rf.patterns_.shape,
                           (n_feats, n_targets, n_delays))

        # we should have np.dot(patterns.T,coef) ~ np.eye(n)
        c0 = rf.coef_.reshape(n_targets, n_feats * n_delays)
        c1 = rf.patterns_.reshape(n_targets, n_feats * n_delays)
        assert_allclose(np.dot(c0, c1.T), np.eye(c0.shape[0]), atol=0.1)

    # Check that warnings are issued when no regularization is applied
    n_feats, n_targets, n_samples = 5, 60, 50
    X, y = make_data(n_feats, n_targets, n_samples, tmin, tmax)
    for estimator in (0., Ridge(alpha=0.)):
        rf = ReceptiveField(tmin, tmax, 1., estimator=estimator, patterns=True)
        with warnings.catch_warnings(record=True) as w:
            rf.fit(y, X)
            # For some reason there is no warning
            if estimator and not check_version('numpy', '1.13'):
                continue
            assert_equal(len(w), 1)
            assert_true(any(x in str(w[0].message).lower()
                            for x in ('singular', 'scipy.linalg.solve')),
                        msg=str(w[0].message))


run_tests_if_main()
