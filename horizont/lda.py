# coding=utf-8
"""
Latent Dirichlet Allocation with Gibbs sampling
"""
from __future__ import absolute_import, division, print_function, unicode_literals
import os
import logging
try:
    from concurrent import futures
except ImportError:
    import futures

import numpy as np
import scipy.special
import sklearn.base
import sklearn.utils

import horizont._lda
import horizont.utils

logger = logging.getLogger('horizont')


class LDA(sklearn.base.BaseEstimator, sklearn.base.TransformerMixin):
    """Latent Dirichlet allocation using Gibbs sampling

    Parameters
    ----------
    n_topics : int
        Number of topics

    n_iter : int, default 1000
        Number of sampling iterations

    alpha : float, default 0.1
        Dirichlet parameter for distribution over topics

    eta : float, default 0.01
        Dirichlet parameter for distribution over words

    random_state : numpy.RandomState | int, optional
        The generator used for the initial topics. Default: numpy.random

    Attributes
    ----------
    `components_` : array, shape = [n_topics, n_features]
        Point estimate of the topic-word distributions (denoted Phi in literature)
    `phi_` : array, shape = [n_topics, n_features]
        Alias for components_
    `nzw_` : array, shape = [n_topics, n_features]
        Matrix of counts recording topic-word assignments in final iteration.
    `theta_` : array, shape = [n_samples, n_features]
        Point estimate of the document-topic distributions (denoted Theta in literature)
    `ndz_` : array, shape = [n_samples, n_features]
        Matrix of counts recording document-topic assignments in final iteration.
    `nz_` : array, shape = [n_topics]
        Array of topic assignment counts in final iteration.

    Examples
    --------
    >>> import numpy as np
    >>> X = np.array([[1,1], [2, 1], [3, 1], [4, 1], [5, 8], [6, 1]])
    >>> from horizont import LDA
    >>> model = LDA(n_topics=2, random_state=0, n_iter=100)
    >>> model.fit(X) #doctest: +ELLIPSIS +NORMALIZE_WHITESPACE
    LDA(alpha=...
    >>> model.components_
    array([[ 0.85714286,  0.14285714],
           [ 0.45      ,  0.55      ]])
    >>> model.loglikelihood() #doctest: +ELLIPSIS
    -40.395...

    Note
    ----
    Although this module uses code written in C (generated by Cython) in its
    implementation of LDA, sampling is *considerably* slower than in pure-Java
    (MALLET_) or pure C implementations (`HCA
    <http://www.mloss.org/software/view/527/>`_). Consider using different
    software if you are working a large dataset.

    References
    ----------
    Blei, David M., Andrew Y. Ng, and Michael I. Jordan. "Latent Dirichlet
    Allocation." Journal of Machine Learning Research 3 (2003): 993–1022.

    Griffiths, Thomas L., and Mark Steyvers. "Finding Scientific Topics."
    Proceedings of the National Academy of Sciences 101 (2004): 5228–5235.
    doi:10.1073/pnas.0307752101.

    Wallach, Hanna, David Mimno, and Andrew McCallum. "Rethinking LDA: Why
    Priors Matter." In Advances in Neural Information Processing Systems 22,
    edited by Y.  Bengio, D. Schuurmans, J. Lafferty, C. K. I. Williams, and A.
    Culotta, 1973–1981, 2009.
    """

    def __init__(self, n_topics=None, n_iter=1000, alpha=0.1, eta=0.01, random_state=None):
        self.n_topics = n_topics
        self.n_iter = n_iter
        self.alpha = alpha
        self.eta = eta
        self.random_state = random_state
        rng = sklearn.utils.check_random_state(random_state)
        # random numbers that are reused
        self._rands = rng.rand(1000)

    def fit(self, X, y=None):
        """Fit the model with X.

        Parameters
        ----------
        X: array-like, shape (n_samples, n_features)
            Training data, where n_samples in the number of samples
            and n_features is the number of features.

        Returns
        -------
        self : object
            Returns the instance itself.
        """
        self._fit(X)
        return self

    def _fit(self, X):
        """Fit the model to the data X.

        Parameters
        ----------
        X: array-like, shape (n_samples, n_features)
            Training vector, where n_samples in the number of samples and
            n_features is the number of features.
        """
        random_state = sklearn.utils.check_random_state(self.random_state)
        X = np.atleast_2d(sklearn.utils.as_float_array(X))
        self._initialize(X, random_state)
        for it in range(self.n_iter):
            if it % 10 == 0:
                self._print_status(it)
            else:
                logger.info("<{}>".format(it))
            self._sample_topics(random_state)
        self._print_status(self.n_iter)
        self.components_ = self.nzw_ + self.eta
        self.components_ /= np.sum(self.components_, axis=1, keepdims=True)
        self.phi_ = self.components_
        self.theta_ = self.ndz_ + self.alpha
        self.theta_ /= np.sum(self.theta_, axis=1, keepdims=True)

        # delete attributes no longer needed after fitting
        del self.WS
        del self.DS
        del self.ZS
        return self

    def _print_status(self, iter):
        ll = self.loglikelihood()
        N = len(self.WS)
        logger.info("<{}> log likelihood: {:.0f}, log perp: {:.4f}".format(iter, ll, -1 * ll / N))

    def _initialize(self, X, random_state):
        D, W = X.shape
        N = int(np.sum(X))
        n_topics = self.n_topics
        n_iter = self.n_iter
        logger.info("n_documents: {}".format(D))
        logger.info("vocab_size: {}".format(W))
        logger.info("n_words: {}".format(N))
        logger.info("n_topics: {}".format(n_topics))
        logger.info("n_iter: {}".format(n_iter))

        self.nzw_ = np.zeros((n_topics, W), dtype=int)
        self.ndz_ = np.zeros((D, n_topics), dtype=int)
        self.nz_ = np.zeros(n_topics, dtype=int)

        # could be moved into Cython
        self.WS, self.DS = horizont.utils.matrix_to_lists(X)
        self.ZS = np.zeros_like(self.WS)
        for i, (w, d) in enumerate(zip(self.WS, self.DS)):
            # random initialization
            # FIXME: improve initialization
            # FIXME: initialization could occur elsewhere
            z_new = random_state.randint(n_topics)
            self.ZS[i] = z_new
            self.ndz_[d, z_new] += 1
            self.nzw_[z_new, w] += 1
            self.nz_[z_new] += 1

    def loglikelihood(self):
        nzw, ndz, nz = self.nzw_, self.ndz_, self.nz_
        alpha = self.alpha
        eta = self.eta
        return self._loglikelihood(nzw, ndz, nz, alpha, eta)

    @staticmethod
    def _loglikelihood(nzw, ndz, nz, alpha, eta):
        """
        Calculate the complete log likelihood, log p(w,z).

        log p(w,z) = log p(w|z) + log p(z)
        """
        D, n_topics = ndz.shape
        vocab_size = nzw.shape[1]
        nd = np.sum(ndz, axis=1)

        ll = 0.0

        # calculate log p(w|z)
        gammaln_eta = scipy.special.gammaln(eta)
        gammaln_alpha = scipy.special.gammaln(alpha)

        ll += n_topics * scipy.special.gammaln(eta * vocab_size)
        for k in range(n_topics):
            ll -= scipy.special.gammaln(eta * vocab_size + nz[k])
            for w in range(vocab_size):
                # if nzw[k, w] == 0 addition and subtraction cancel out
                if nzw[k, w] > 0:
                    ll += scipy.special.gammaln(eta + nzw[k, w]) - gammaln_eta

        # calculate log p(z)
        for d in range(D):
            ll += scipy.special.gammaln(alpha * n_topics) - scipy.special.gammaln(alpha * n_topics + nd[d])
            for k in range(n_topics):
                if ndz[d, k] > 0:
                    ll += scipy.special.gammaln(alpha + ndz[d, k]) - gammaln_alpha
        return ll

    def _sample_topics(self, random_state):
        random_state = sklearn.utils.check_random_state(self.random_state)
        rands = self._rands
        random_state.shuffle(rands)
        n_topics, vocab_size = self.nzw_.shape
        alpha = np.repeat(self.alpha, n_topics)
        eta = np.repeat(self.eta, vocab_size)
        horizont._lda._sample_topics(self.WS, self.DS, self.ZS,
                                     self.nzw_, self.ndz_, self.nz_,
                                     alpha, eta, rands)

    def transform(self, X, y=None):
        """Transform the data X according to the fitted model

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            New data, where n_samples in the number of samples
            and n_features is the number of features.

        Returns
        -------
        theta : array-like, shape (n_samples, n_topics)
            Point estimate of the document-topic distributions

        """
        raise NotImplementedError

    def fit_transform(self, X, y=None):
        """Apply dimensionality reduction on X.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features)
            New data, where n_samples in the number of samples
            and n_features is the number of features.

        Returns
        -------
        theta : array-like, shape (n_samples, n_topics)
            Point estimate of the document-topic distributions

        """
        self._fit(np.atleast_2d(X))
        return self.theta_

    def score(self, X, R, random_state=None):
        """
        Calculate marginal probability of observations in X given Phi.

        Returns a list with estimates for each document separately, mimicking
        the behavior of scikit-learn. Uses Buntine's left-to-right sequential sampler.

        Parameters
        ----------
        X : array, [n_samples, n_features]
            The document-term matrix of documents for evaluation.

        R : int
            The number of particles to use for the estimation.

        Returns
        -------
        logprobs : array of length n_samples
            Estimate of marginal log probability for each row of X.
        """
        if random_state is None:
            random_state = sklearn.utils.check_random_state(self.random_state)
            rands = self._rands
        else:
            random_state = sklearn.utils.check_random_state(random_state)
            # get rands in a known state
            rands = np.sort(self._rands)
            random_state.shuffle(rands)

        N, V = X.shape
        Phi, alpha = self.phi_, self.alpha
        np.testing.assert_equal(V, Phi.shape[1])

        # calculate marginal probability for each document separately
        logprobs = []
        multiprocessing = int(os.environ.get('JOBLIB_MULTIPROCESSING', 1)) or None
        if multiprocessing:
            with futures.ProcessPoolExecutor() as ex:
                futs = []
                for x in X:
                    # for consistency one needs to pass a copy of rands
                    # the executor takes time to pickle; it's not instantaneous
                    futs.append(ex.submit(horizont._lda._score_doc, x, Phi, alpha, R, rands.copy()))
                    random_state.shuffle(rands)
                for i, fut in enumerate(futs):
                    logprobs.append(fut.result())
        else:
            for x in X:
                logprobs.append(horizont._lda._score_doc(x, Phi, alpha, R, rands))
                random_state.shuffle(rands)
        return logprobs
