import numpy as np
from scipy import linalg
from pyscf import lib
from pyscf.mcscf.addons import StateAverageMCSCFSolver, StateAverageMixFCISolver
from pyscf.fci import direct_spin1
from mrh.my_pyscf import mcpdft
# API for general state-interaction MC-PDFT method object
# In principle, various forms can be implemented: CMS, XMS, etc.

def make_ham_si (mc,ci):
    ''' Build Hamiltonian matrix in basis of ci vector, with diagonal elements
        computed by PDFT and off-diagonal elements computed by MC-SCF '''

    ci = np.asarray(ci)
    nroots = ci.shape[0]

    e_pdft = np.stack ([mcpdft.mcpdft.kernel (mc, ot=mc.otfnal, ci=ci, root=i)
        for i in range (nroots)], axis=1)
    e_int, e_ot = e_pdft

    h1, h0 = mc.get_h1eff ()
    h2 = mc.get_h2eff ()
    h2eff = direct_spin1.absorb_h1e (h1, h2, mc.ncas, mc.nelecas, 0.5)
    hc_all = [direct_spin1.contract_2e (h2eff, c, mc.ncas, mc.nelecas) for c in ci]
    ham_si = np.tensordot (ci, hc_all, axes=((1,2),(1,2))) 
    e_cas = ham_si.diagonal ().copy ()
    e_mcscf = e_cas + h0
    ham_si[np.diag_indices (nroots)] = e_int[:].copy ()
    ci_flat = ci.reshape (nroots, -1)
    ovlp_si = np.dot (ci_flat.conj (), ci_flat.T)
    return ham_si, ovlp_si, e_mcscf, e_cas, e_ot

def si_newton (mc, ci=None, max_cyc=None, conv_tol=None, sing_tol=None, nudge_tol=None):

    if ci is None: ci = mc.ci
    if max_cyc is None: max_cyc = getattr (mc, 'max_cyc_sarot', 50)
    if conv_tol is None: conv_tol = getattr (mc, 'conv_tol_sarot', 1e-8)
    if sing_tol is None: sing_tol = getattr (mc, 'sing_tol_sarot', 1e-8)
    if nudge_tol is None: nudge_tol = getattr (mc, 'nudge_tol_sarot', 1e-3)
    ci_old = np.array (ci)
    log = lib.logger.new_logger (mc, mc.verbose)
    nroots = mc.fcisolver.nroots 
    rows,col = np.tril_indices(nroots,k=-1)
    npairs = nroots * (nroots - 1) // 2
    t = np.zeros((nroots,nroots))
    conv = False
    hdr = '{}-PDFT intermediate-state'.format (mc.sarot_name)

    for it in range(max_cyc):
        log.info ("****iter {} ***********".format (it))

#       Form U
        U = linalg.expm(t)

#       Rotate T
        try:
            ci = np.tensordot(U, ci, 1)
        except ValueError as e:
            print (U.shape, ci.shape)
            raise (e)

        f, df, d2f = mc.sarot_objfn (ci=ci)
        log.info ("{} objective function value = {}".format (hdr, f))

        # Analyze Hessian
        d2f, evecs = linalg.eigh (d2f)
        evecs = np.array(evecs)
        if np.any (np.abs (d2f) < sing_tol):
            log.warn ("{} Hess is singular!".format (hdr))
        pos_idx = d2f > 0
        neg_def = np.all (d2f < 0)
        log.info ("{} Hessian is negative-definite? {}".format (hdr, neg_def))

        # Analyze gradient
        grad_norm = np.linalg.norm(df)
        log.info ("{} grad norm = %f".format (hdr), grad_norm)
        df = np.dot (df, evecs)
        log.info ("{} grad (normal modes) = {}".format (hdr, df))

        # Take step
        df[pos_idx & (np.abs (df/d2f) < nudge_tol)] = nudge_tol
        Dt = df/np.abs (d2f)
        step_norm = np.linalg.norm (Dt)
        log.info ("{} Hessian eigenvalues: {}".format (hdr, d2f))
        log.info ("{} step vector (normal modes): {}".format (hdr, Dt))
        t[:] = 0
        t[np.tril_indices(t.shape[0], k = -1)] = np.dot (Dt, evecs.T)
        t = t - t.T

        if grad_norm < conv_tol and step_norm < conv_tol and neg_def == True:
                conv = True
                break

    U_signed = np.tensordot (ci_old, ci.conj (), axes=((1,2),(1,2)))
    if mc.verbose >= lib.logger.DEBUG:
        fmt_str = ' ' + ' '.join (['{:5.2f}',]*nroots)
        log.debug ("{} final overlap matrix:".format (hdr))
        for row in U_signed: log.debug (fmt_str.format (*row))
    # Root order and sign by overlap criterion
    # Requires ~strictly~ non-repeating sort
    # TODO: generalize to only sort within solvers in
    # SA-mix (can probably hack this using U_abs)
    #U_abs = np.abs (U_signed)
    #sgn = np.ones (nroots)
    #ovlp_idx = -sgn.copy ().astype (np.int32)
    #for imax in range (nroots):
    #    i = np.argmax (U_abs)
    #    j, k = i // nroots, i % nroots
    #    sgn[j] -= 2 * int (U_signed[j,k] < 0)
    #    ovlp_idx[j] = k
    #    U_abs[j,:] = -1
    #log.debug ("{} sign-permutation array: {}".format (hdr, sgn))
    #log.debug ("{} overlap sort array: {}".format (hdr, ovlp_idx))
    #ci *= sgn[:,None,None]
    #ci = ci[ovlp_idx,:,:]

    if conv:
        log.note ("{} optimization CONVERGED".format (hdr))
    else:
        log.note (("{} optimization did not converge after {} "
                   "cycles".format (hdr, it)))


    return list (ci)

class StateInteractionMCPDFTSolver ():
    pass
    # tag

class _SIPDFT (StateInteractionMCPDFTSolver):
    ''' I'm not going to use subclass to distinguish between various SI-PDFT
        types. Instead, I'm going to use three method attributes:

        _sarot_objfn : callable
            Args: ci vectors
            Returns: float, array (nroots), array (nroots,nroots)
            The value, first, and second derivatives of the objective function
            which extrema define the intermediate states. May be used both in
            performing the SI-PDFT energy calculation and in gradients.

        _sarot : callable
            Args: ci vectors
            Returns: ci vectors
            Obtain the intermediate states from the reference states

        sarot_name: string
            Label for I/O.
    '''

    # Metaclass parent

    def __init__(self, mc, sarot_objfn, sarot, sarot_name):
        self.__dict__.update (mc.__dict__)
        keys = set (('sarot_objfn', 'sarot', 'sarot_name', 'ham_si', 'si', 'max_cycle_sarot', 'conv_tol_sarot'))
        self._sarot_objfn = sarot_objfn
        self._sarot = sarot
        self.max_cycle_sarot = 50
        self.conv_tol_sarot = 1e-8
        self.si = self.ham_si = None
        self.sarot_name = sarot_name
        self._keys = set ((self.__dict__.keys ())).union (keys)

    @property
    def e_states (self):
        return getattr (self, '_e_states', self.fcisolver.e_states)
    @e_states.setter
    def e_states (self, x):
        self._e_states = x
    ''' Unfixed to FCIsolver since SI-PDFT state energies are no longer
        CI solutions '''

    @property
    def ham_ci (self):
        ham_ci = self.ham_si.copy ()
        nroots = ham_ci.shape[0]
        ham_ci[np.diag_indices (nroots)] = self.e_mcscf.copy ()
        return ham_ci
    
    def _init_ci0 (self, ci0, mo_coeff=None):
        ''' On the assumption that ci0 represents states that optimize the
            SI-PDFT objective function, prediagonalize the Hamiltonian so that
            the MC-SCF step has a better initialization. '''
        # TODO: different spin states in state-average-mix
        if ci0 is None: return None
        if mo_coeff is None: mo_coeff = self.mo_coeff
        ncas, nelecas = self.ncas, self.nelecas
        h1, h0 = self.get_h1eff (mo_coeff)
        h2 = self.get_h2eff (mo_coeff)
        h2eff = direct_spin1.absorb_h1e (h1, h2, ncas, nelecas, 0.5)
        hc_all = [direct_spin1.contract_2e (h2eff, c, ncas, nelecas) for c in ci0]
        ham_ci = np.tensordot (np.asarray (ci0), hc_all, axes=((1,2),(1,2)))
        e, u = linalg.eigh (ham_ci)
        ci = list (np.tensordot (u.T, np.asarray (ci0), axes=1))
        return ci

    def _init_sarot_ci (self, ci, ci0):
        ''' On the assumption that ci0 represents states that optimize the
            SI-PDFT objective function, rotate the MC-SCF ci vectors to maximize
            their overlap with ci0 so that the sarot step has a better
            initialization.'''
        # TODO: different spin states in state-average-mix
        if ci0 is None: return None
        ci0_array = np.asarray (ci0)
        ci_array = np.asarray (ci)
        ovlp = np.tensordot (ci0_array.conj (), ci_array, axes=((1,2),(1,2)))
        u, svals, vh = linalg.svd (ovlp)
        ci = list (np.tensordot (u @ vh, ci_array, axes=1))
        return ci

    def kernel (self, mo_coeff=None, ci0=None, **kwargs):
        # I should maybe rethink keeping all this intermediate information
        self.otfnal.reset (mol=self.mol) # scanner mode safety 
        ci = self._init_ci0 (ci0, mo_coeff=mo_coeff)
        ci, self.mo_coeff, self.mo_energy = super().kernel (mo_coeff, ci, **kwargs)[-3:]
        ci = self._init_sarot_ci (ci, ci0)
        self.ci = self.sarot (ci=ci, **kwargs)
        self.ham_si, self.ovlp_si, self.e_mcscf, self.e_ot, self.e_cas = self.make_ham_si (self.ci)
        self._log_sarot ()
        self.e_states, self.si = self._eig_si (self.ham_si)
        # TODO: state_average_mix support
        self.e_tot = np.dot (self.e_states, self.weights)
        self._log_si ()
        return self.e_tot, self.e_ot, self.e_mcscf, self.e_cas, self.ci, self.mo_coeff, self.mo_energy

    # All of the below probably need to be wrapped over solvers in state-interaction-mix metaclass

    def sarot (self, ci=None):
        ''' Obtain intermediate states in the average space '''
        if ci is None: ci = self.ci
        return self._sarot (self, ci)

    def sarot_objfn (self, mo_coeff=None, ci=None):
        ''' The value, first, and second-derivative matrix of the objective
            function rendered stationary by the intermediate states. Used
            in gradient calculations and possibly in sarot. '''
        if mo_coeff is None: mo_coeff = self.mo_coeff
        if ci is None: ci = self.ci
        return self._sarot_objfn (self, mo_coeff=mo_coeff, ci=ci)

    def _eig_si (self, ham_si):
        return linalg.eigh (ham_si)

    def make_ham_si (self, ci=None):
        if ci is None: ci = self.ci
        return make_ham_si (self, ci)

    def _log_sarot (self):
        e_pdft = self.ham_si.diagonal ()
        nroots = len (e_pdft)
        log = lib.logger.new_logger (self, self.verbose)
        f, df, d2f = self.sarot_objfn ()
        log.note ('%s-PDFT intermediate objective function  value = %.15g  |grad| = %.7g',
            self.sarot_name, f, linalg.norm (df))
        log.note ('%s-PDFT intermediate average energy  EPDFT = %.15g  EMCSCF = %.15g',
            self.sarot_name, np.dot (self.weights, e_pdft),
            np.dot (self.weights, self.e_mcscf))
        log.note ('%s-PDFT intermediate states:', self.sarot_name)
        if getattr (self.fcisolver, 'spin_square', None):
            ss = self.fcisolver.states_spin_square (self.ci, self.ncas,
                                                    self.nelecas)[0]
            for i in range (nroots):
                log.note ('  State %d weight %g  EPDFT = %.15g  EMCSCF = %.15g  S^2 = %.7f',
                    i, self.weights[i], e_pdft[i], self.e_mcscf[i], ss[i])
        else:
            for i in range (nroots):
                log.note ('  State %d weight %g  EPDFT = %.15g  EMCSCF = %.15g',
                    i, self.weights[i], e_pdft[i], self.e_mcscf[i])
        log.info ('Intermediate state Hamiltonian matrix:')
        fmt_str = ' '.join (['{:9.5f}',]*nroots)
        for row in self.ham_si: log.info (fmt_str.format (*row))
        log.info ('Intermediate states (columns) in terms of reference states (rows):')
        e, v = self._eig_si (self.ham_ci)
        for row in v.T: log.info (fmt_str.format (*row))

    def _log_si (self):
        ''' Information about the final states '''
        log = lib.logger.new_logger (self, self.verbose)
        e_pdft = self.e_states
        nroots = len (e_pdft)
        e_mcscf = (np.dot (self.ham_ci, self.si) * self.si.conj ()).sum (0)
        log.note ('%s-PDFT final states:', self.sarot_name) 
        if getattr (self.fcisolver, 'spin_square', None):
            ci = np.tensordot (self.si.T, np.asarray (self.ci), axes=1)
            ss = self.fcisolver.states_spin_square (ci, self.ncas,
                                                    self.nelecas)[0]
            for i in range (nroots):
                log.note ('  State %d weight %g  EPDFT = %.15g  EMCSCF = %.15g  S^2 = %.7f',
                    i, self.weights[i], e_pdft[i], self.e_mcscf[i], ss[i])
        else:
            for i in range (nroots):
                log.note ('  State %d weight %g  EPDFT = %.15g  EMCSCF = %.15g',
                    i, self.weights[i], e_pdft[i], self.e_mcscf[i])

    def nuc_grad_method (self):
        from mrh.my_pyscf.grad.sipdft import Gradients
        return Gradients (self)

def get_sarotfns (obj):
    if obj.upper () == 'CMS':
        from mrh.my_pyscf.mcpdft.cmspdft import e_coul as sarot_objfn
        sarot = si_newton
    else:
        raise RuntimeError ('SI-PDFT type not supported')
    return sarot_objfn, sarot

def state_interaction (mc, weights=(0.5,0.5), obj='CMS', **kwargs):
    ''' Build state-interaction MC-PDFT method object

    Args:
        mc : instance of class _PDFT
    
    Kwargs:
        weights : sequence of floats
        obj : objective-function type
            Currently supports only 'cms'

    Returns:
        si : instance of class _SIPDFT '''

    if isinstance (mc, StateInteractionMCPDFTSolver):
        raise RuntimeError ('state_interaction recursion! possible API bug!')
    if isinstance (mc.fcisolver, StateAverageMixFCISolver):
        raise RuntimeError ('TODO: state-average mix support')
    if not isinstance (mc, StateAverageMCSCFSolver):
        mc = mc.state_average (weights=weights, **kwargs)
    mcbase_class = mc.__class__
    sarot_objfn, sarot = get_sarotfns (obj)

    class SIPDFT (_SIPDFT, mcbase_class):
        pass
    return SIPDFT (mc, sarot_objfn, sarot, obj)
    

if __name__ == '__main__':
    # This ^ is a convenient way to debug code that you are working on. The
    # code in this block will only execute if you run this python script as the
    # input directly: "python sipdft.py".

    from pyscf import scf, gto
    from mrh.my_pyscf.tools import molden # My version is better for MC-SCF
    from mrh.my_pyscf.fci import csf_solver
    xyz = '''O  0.00000000   0.08111156   0.00000000
             H  0.78620605   0.66349738   0.00000000
             H -0.78620605   0.66349738   0.00000000'''
    mol = gto.M (atom=xyz, basis='sto-3g', symmetry=False, output='sipdft.log', verbose=lib.logger.DEBUG)
    mf = scf.RHF (mol).run ()
    mc = mcpdft.CASSCF (mf, 'tPBE', 4, 4).set (fcisolver = csf_solver (mol, 1))
    mc = mc.state_interaction ([1.0/3,]*3, 'cms').run ()


