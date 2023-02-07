import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import minimize_scalar
from matplotlib.backends.backend_pdf import PdfPages
from joblib import Parallel, delayed
import warnings
from tqdm import tqdm
from multiprocessing import cpu_count
try:
    from statsmodels.nonparametric.kernel_regression import KernelReg
except ModuleNotFoundError:
    print('No module named: statsmodels')
    print('Install it if you wish to use fittype = \'nonparametric\'!')

from scipy.stats import binned_statistic
from scipy.optimize import minimize

def chi2model(params,x,y,sig,pol):
    xoffset = params[0]
    yoffset = params[1]
    model = pol(x + xoffset) + yoffset
    chi2 = (np.power(y - model,2) / (sig**2)).sum()

    return chi2

def mintime_parallel(params):
    """
    Refit minima with generating new observations from noise
    """
    fittype = params[-1]
    if fittype=='model':
        x,y,err,pol,zero_time,x0,y0 = params[:7]
        phaseoffset,i,period = params[7:-1]
    else:
        x,y,order,zero_time = params[:4]
        bound1, bound2 = params[4:-1]

    if fittype=='nonparametric':
        ksrmv = KernelReg(endog=y, exog=x, var_type='c',
                          reg_type='ll', bw=np.array([np.median(np.diff(x))]) )
        p_fit = lambda x : ksrmv.fit(np.atleast_1d(x))[0][0] if isinstance(x,float) else ksrmv.fit(np.atleast_1d(x))[0]

        result = minimize_scalar(p_fit, bounds=(bound1, bound2), method='bounded')
        t = result.x + zero_time
    elif fittype=='model':
        res = minimize(chi2model, (x0,y0), args=(x,y,err,pol) ,
                       method='Powell')

        #yoffset = res.x[1]
        xoffset = res.x[0]

        t = zero_time +phaseoffset +(i-1)*period -xoffset
    else:
        with warnings.catch_warnings(record=True):
            y_model = np.polyfit(x,y,order)
            p_fit = np.poly1d(y_model)

        result = minimize_scalar(p_fit, bounds=(bound1, bound2), method='bounded')
        t = result.x + zero_time

    return t

class OCFitter:
    def __init__(self,time,flux,fluxerror,period):
        '''
        time : array
            Light curve time points.
        flux : array
            Corresponding flux/mag values.
        fluxerror : array, optional
            Corresponding flux/mag error values.
        period : float
            Period of given variable star.
        '''

        self.period = float(period)

        time = np.asarray(time,dtype=float)
        flux = np.asarray(flux,dtype=float)
        fluxerror = np.asarray(fluxerror,dtype=float)

        goodpts = np.isfinite(time)
        goodpts &= np.isfinite(flux)
        goodpts &= np.isfinite(fluxerror)

        self.x = time[goodpts]
        self.y = flux[goodpts]
        self.err = fluxerror[goodpts]

    def get_model(self,phase=0,show_plot=False):
        times = self.x.copy()
        zero_time = np.floor(times[0])
        times -= zero_time

        flux = self.y.copy()
        corrflux = flux.copy()

        lcmean = np.mean(flux) - np.ptp(flux)/2

        period = self.period

        # Loop over each cycle and shift them vertically to match each other
        i = 0
        while True:
            um = np.where(( i*period <= times) & (times < period + i*period)  )[0]

            if len(um)==0:
                if i*period > times.max():
                    break
                else:
                    i += 1
                    continue
            corrflux[um] -= corrflux[um].min()
            corrflux[um] += lcmean
            i += 1

            if i*period > times.max():
                break

        # Shift minimum to middle of the phase curve
        times -= phase
        times += period/2

        # Bin phase shifted phase curve
        ybinned,xbinned,_ = binned_statistic(times%period,corrflux,statistic='median', bins=100, range=(0,period))
        xbinned = (xbinned[1:] + xbinned[:-1])/2

        xbinned += phase
        xbinned -= period/2

        goodpts = np.where( np.isfinite(ybinned) )[0]
        xbinned = xbinned[goodpts]
        ybinned = ybinned[goodpts]

        # Get model fit
        ksrmv = KernelReg(endog=ybinned, exog=xbinned, var_type='c',
                          reg_type='ll', bw=np.array([0.02]) )
        pol = lambda x : ksrmv.fit(np.atleast_1d(x))[0][0] if isinstance(x,float) else ksrmv.fit(np.atleast_1d(x))[0]

        if show_plot:
            phasetoplot = times%period +phase -period/2

            plt.figure(figsize=(12,8))
            ax = plt.subplot(111)
            plt.title('Light curve model to be shifted to each minimum')
            plt.plot( phasetoplot, flux , '.', c='lightgray',label='Original data')
            plt.plot( phasetoplot, corrflux , 'k.',label='Veritically shifted')
            plt.plot(xbinned,ybinned,'.',label='Binned')
            plt.plot( np.linspace(phasetoplot.min(),phasetoplot.max(),100),pol(np.linspace(phasetoplot.min(),phasetoplot.max(),100)) ,label='Model')
            plt.xlabel('Cycle (= one period)')
            plt.ylabel('Brightness')
            # Shrink current axis by 20%
            box = ax.get_position()
            ax.set_position([box.x0, box.y0, box.width * 0.8, box.height])

            # Put a legend to the right of the current axis
            ax.legend(loc='center left', bbox_to_anchor=(1, 0.5))
            plt.show()

        return pol , phase

    def fit_minima(self,
                    fittype='poly',
                    phase_interval=0.1,
                    order=3,smoothness=1,
                    npools=-1,samplings=100000,
                    showplot=False,saveplot=True,showfirst=True,
                    filename='',
                    debug=False):
        """
        Fit all minima(!) one by one.

        Parameters
        ----------
        fittype : 'poly', 'nonparametric' or 'model'.
            The type of the fitted function.
        phase_interval : float
            The phase interval around expected minima, which is
            used to fit a function.
        order : int
            Order of the polynomial to be fitted to each minimum.
            Applies only if `fittype` is `poly`.
        smoothness : float
            The smoothness of fitted nonparametric function.
            Use ~1, to follow small scale variations. Use >1
            to fit a really smooth function.
            Applies only if `fittype` is `nonparametric`.
        npools : int, default: -1
            Number of cores during error estimation.
            If '-1', then all cores are used.
        samplings : int, default: 100000
            Number of resamplings for error estimation.
        showplot : bool, default: False
            Show all plots.
        saveplot : bool, default: True
            Save all plots.
        filename : str, default: ''
            Filename to be used to save plots.
        showfirst : bool, default: True
            Show epoch and first fit to check parameters.
        """

        if fittype not in ['poly','nonparametric','model']:
            raise NameError('Fittype is not known! Use \'poly\', \'nonparametric\' or \'model\'.')

        if npools == -1:
            npools = cpu_count()

        print('Calculating minima times...')
        if saveplot: pp = PdfPages(filename+'_minima_fit.pdf')

        x = self.x
        y = self.y
        err = self.err
        period = self.period

        cadence = np.median(np.diff(x))

        # List to store times of minima
        mintimes = []

        #####################################
        # Fit first cycle to estimate epoch #
        #####################################
        umcycle = np.where(x<x[0]+period)[0]
        um2 = np.argmin( y[umcycle] )

        # If very first point is the min, extend phase interval
        if um2 == 0:
            umcycle = np.where(x<x[0]+1.3*period)[0]
            um2 = np.argmin( y[umcycle] )

            mean_t = x[umcycle][um2]
        else:
            mean_t = x[umcycle][um2]

        zero_time = np.floor(x[0])

        # Fit the data within this phase interval
        pm = period*0.1 #days

        um=np.where((mean_t-pm<x) & (x<=mean_t+pm)  )[0]

        if fittype=='nonparametric':
            ksrmv = KernelReg(endog=y[um], exog=x[um]-zero_time, var_type='c',
                              reg_type='ll', bw=np.array([np.median(np.diff(x[um]))]) )
            p = lambda x : ksrmv.fit(np.atleast_1d(x))[0][0] if isinstance(x,float) else ksrmv.fit(np.atleast_1d(x))[0]
        else:
            with warnings.catch_warnings(record=True):
                z = np.polyfit(x[um]-zero_time, y[um], 5)
            p = np.poly1d(z)

        result = minimize_scalar(p, bounds=(mean_t-zero_time-pm, mean_t-zero_time+pm), method='bounded')
        mean_t = result.x + zero_time

        if showplot or showfirst:
            plt.figure(figsize=(8,6))
            plt.scatter(x[umcycle],y[umcycle],c='k',label='First cycle')
            plt.plot(x[um],p(x[um]-zero_time))
            plt.axvline( mean_t, c='r',label='Epoch',zorder=0 )
            plt.xlabel('Time')
            plt.ylabel('Brightness')
            plt.suptitle('Estimating epoch: '+filename)
            plt.legend()
            plt.show()

        #######################################
        # Fit each cycle to get minimum times #
        #######################################

        # Initialize progress bar
        pbar = tqdm()

        # Range to be fitted around the expected minimum:
        pm = period*phase_interval #days

        if fittype=='model':
            pol,phaseoffset = self.get_model(phase=mean_t-zero_time,
                                             show_plot=showfirst or showplot)

        i=1 #First minimum
        firstmin = True
        while True:
            # If duty cycle is lower than 20% do not fit
            dutycycle = 0.2
            um = np.where((mean_t-pm<x) & (x<=mean_t+pm)  )[0]
            if len(x[um])<(pm/cadence*dutycycle):
                mean_t = mean_t + period
                i=i+1
                if mean_t> np.max(x):
                    break
                else:
                    continue

            ###################################################
            # First fit the data around expected minimum time #
            ###################################################
            if fittype=='nonparametric':
                ksrmv = KernelReg(endog=y[um], exog=x[um]-zero_time, var_type='c',
                                  reg_type='ll', bw=np.array([np.median(np.diff(x[um]))]) )
                p = lambda x : ksrmv.fit(np.atleast_1d(x))[0][0] if isinstance(x,float) else ksrmv.fit(np.atleast_1d(x))[0]

                result = minimize_scalar(p, bounds=(mean_t-zero_time-pm, mean_t-zero_time+pm), method='bounded')
                t_initial = result.x + zero_time
            elif fittype=='model':
                with warnings.catch_warnings(record=True):
                    y0 = np.mean(y[um]) - np.mean(pol(x[um]-zero_time -(i-1)*period))
                    x0 = 0
                    res = minimize(chi2model, (x0,y0),
                                   args=(x[um]-zero_time -(i-1)*period , y[um],err[um],pol),
                                   method='Powell')
                                   #bounds=((-period/2,period/2),(-np.inf,np.inf)))

                yoffset = res.x[1]
                xoffset = res.x[0]

                t_initial = zero_time +phaseoffset +(i-1)*period -xoffset

                if debug:
                    plt.title('First fit')
                    plt.plot(x[um],y[um],'.')
                    xtobeplotted = np.linspace( x[um].min(),x[um].max(), 1000 )
                    plt.plot(xtobeplotted,pol(xtobeplotted-zero_time-(i-1)*period+x0 ) + y0 ,'r',label='Initial')
                    plt.plot(xtobeplotted,pol(xtobeplotted-zero_time-(i-1)*period + xoffset) + yoffset,'k',label='Final fit')
                    plt.axvline(t_initial,c='r')
                    plt.xlim(x[um][0],x[um][-1])
                    #plt.ylim(y[um].min() - 0.1*y[um].ptp(), y[um].max() + 0.1*y[um].ptp() )
                    plt.legend()
                    plt.show()
                    plt.close('all')
            else:
                with warnings.catch_warnings(record=True):
                    z = np.polyfit(x[um]-zero_time, y[um], order)
                p = np.poly1d(z)

                result = minimize_scalar(p, bounds=(mean_t-zero_time-pm, mean_t-zero_time+pm), method='bounded')
                t_initial = result.x + zero_time

                if debug:
                    plt.title('First fit')
                    plt.plot(x[um],y[um],'.')
                    xtobeplotted = np.linspace( x[um].min(),x[um].max(), 1000 )
                    plt.plot(xtobeplotted,p(xtobeplotted-zero_time ) ,'r',label='Polyfit')
                    plt.axvline(t_initial,c='r')
                    plt.xlim(x[um][0],x[um][-1])
                    #plt.ylim(y[um].min() - 0.1*y[um].ptp(), y[um].max() + 0.1*y[um].ptp() )
                    plt.legend()
                    plt.show()
                    plt.close('all')


            # Continue if duty cycle is lower than 20%
            um = np.where((t_initial-pm<x) & (x<=t_initial+pm)  )
            um_before = np.where((t_initial-pm<x) & (x<=t_initial)  )
            um_after  = np.where((t_initial<x) & (x<=t_initial+pm)  )
            if len(x[um_before])<(pm/cadence*dutycycle) or len(x[um_after])<(pm/cadence*dutycycle):
                mean_t = mean_t + period
                i=i+1
                continue

            ########################################################
            # Second fit the data again around fitted minimum time #
            ########################################################
            if fittype=='nonparametric':
                ksrmv = KernelReg(endog=y[um], exog=x[um]-zero_time, var_type='c',
                                  reg_type='ll', bw=smoothness*np.array([np.median(np.diff(x[um]))]) )
                p = lambda x : ksrmv.fit(np.atleast_1d(x))[0][0] if isinstance(x,float) else ksrmv.fit(np.atleast_1d(x))[0]

                result = minimize_scalar(p, bounds=(t_initial-zero_time-pm, t_initial-zero_time+pm), method='bounded')
                t = result.x + zero_time
            elif fittype=='model':
                with warnings.catch_warnings(record=True):
                    y0 = yoffset
                    x0 = xoffset
                    res = minimize(chi2model, (x0,y0),
                                   args=(x[um]-zero_time-(i-1)*period , y[um],err[um],pol),
                                   method='Powell')
                                   #bounds=((-period/2,period/2),(-np.inf,np.inf)))

                yoffset = res.x[1]
                xoffset = res.x[0]

                t = zero_time +phaseoffset +(i-1)*period -xoffset

                if debug:
                    plt.title('Second fit')
                    plt.plot(x[um],y[um],'.')
                    xtobeplotted = np.linspace( x[um].min(),x[um].max(), 1000 )
                    plt.plot(xtobeplotted,pol(xtobeplotted-zero_time-(i-1)*period +x0)+y0 ,label='Initial')
                    plt.plot(xtobeplotted,pol(xtobeplotted-zero_time-(i-1)*period + xoffset) + yoffset ,label='Final fit')
                    plt.axvline(t,c='r')
                    plt.legend()
                    plt.show()
                    plt.close('all')
            else:
                with warnings.catch_warnings(record=True):
                    z = np.polyfit(x[um]-zero_time, y[um], order)
                p = np.poly1d(z)

                result = minimize_scalar(p, bounds=(t_initial-zero_time-pm, t_initial-zero_time+pm), method='bounded')
                t = result.x + zero_time


                if debug:
                    plt.title('Second fit')
                    plt.plot(x[um],y[um],'.')
                    xtobeplotted = np.linspace( x[um].min(),x[um].max(), 1000 )
                    plt.plot(xtobeplotted,p(xtobeplotted-zero_time ) ,'r',label='Polyfit')
                    plt.axvline(t_initial,c='r')
                    plt.xlim(x[um][0],x[um][-1])
                    #plt.ylim(y[um].min() - 0.1*y[um].ptp(), y[um].max() + 0.1*y[um].ptp() )
                    plt.legend()
                    plt.show()
                    plt.close('all')

            #######################
            # Stopping conditions #
            #######################
            # Break if the data is over
            if mean_t> np.max(x):
                break

            # Continue if the number of points is low (duty cycle is lower than 20%)
            um_before = np.where((t-pm<x) & (x<=t)  )
            um_after = np.where((t<x) & (x<=t+pm)  )
            if len(x[um_before])<(pm/cadence*0.2) or len(x[um_after])<(pm/cadence*0.2) or len(x[um])<(pm/cadence*0.2):
                mean_t = mean_t + period
                i=i+1
                continue

            # Continue if fit is not a minimum
            first_point = y[um][0]
            last_point = y[um][-1]
            middle_point = np.min(y[um][1:-1])
            if not (middle_point<=first_point and middle_point<=last_point):
                mean_t = mean_t + period
                i=i+1
                continue

            ###########################################################
            # Calculate error by sampling from y errors and refitting #
            ###########################################################
            z_fit_parallel = []
            if fittype=='model':
                for _ in range(samplings):
                    y_resampled = y[um] + np.random.normal(loc=0,scale=err[um],size=err[um].shape[0])
                    z_fit_parallel.append([x[um]-zero_time-(i-1)*period, y_resampled, err[um], pol, zero_time,
                                           xoffset, yoffset, phaseoffset, i, period, fittype ])
            else:
                for _ in range(samplings):
                    y_resampled = y[um] + np.random.normal(loc=0,scale=err[um],size=err[um].shape[0])
                    z_fit_parallel.append([x[um]-zero_time, y_resampled, order, zero_time, t-zero_time-pm, t-zero_time+pm , fittype ])

            t_trace = Parallel(n_jobs=npools)(delayed(mintime_parallel)(par) for par in z_fit_parallel)
            t_trace = np.array(t_trace)

            try:
                del z_fit_parallel
                del y_resampled

                OC_err = np.median(t_trace)-np.percentile(t_trace,15.9)
            except UnboundLocalError:
                OC_err = 0

            #Append minimum time
            mintimes.append([t,OC_err])

            ################
            # Plot the fit #
            ################
            #plt.plot(x-zero_time,y,'o',c='gray')
            plt.errorbar(x[um]-zero_time,y[um],yerr=err[um],color='k',fmt='.',zorder=0)
            #plt.plot(x[um_before]-zero_time,y[um_before],'m.',zorder=5)
            if fittype=='model':
                xtobeplotted = np.linspace( x[um].min(),x[um].max(), 1000 )
                plt.plot(xtobeplotted-zero_time,pol(xtobeplotted-zero_time +xoffset -(i-1)*period)+yoffset ,c='r',zorder=10)
            else:
                xtobeplotted = np.linspace( (x[um]-zero_time).min(),(x[um]-zero_time).max(), 1000 )
                plt.plot(xtobeplotted,p(xtobeplotted),c='r',zorder=10)
            plt.axvline(t-zero_time,zorder=0,label='Observed min')
            if firstmin:
                epoch = t-zero_time-(i-1)*period
            else:
                plt.axvline(epoch+(i-1)*period,c='lightgray',zorder=0,label='Calculated')
            #plt.axvline(t_initial_final-zero_time)
            plt.xlabel('Time')
            plt.ylabel('Brightness')
            plt.legend()
            if saveplot: plt.savefig(pp,format='pdf',dpi=300)
            if showplot or (firstmin and showfirst): plt.show()
            plt.close()

            firstmin = False

            ############################
            # Step to the next minimum #
            ############################
            pbar.update()

            mean_t = mean_t + period
            i=i+1
            #If the data is over, break
            if mean_t > np.max(x):
                break

        pbar.close()

        if saveplot: pp.close()

        mintimes = np.array(mintimes)
        time_of_minimum = mintimes[:,0]
        err_of_minimum = mintimes[:,1]

        return time_of_minimum,err_of_minimum

    def calculate_OC(self,min_times,period,t0=None,min_times_err=None,saveplot=False,showplot=False,saveOC=True,filename=''):
        """
        Calculate O-C curve from given period and minimum times.

        Parameters
        ----------
        min_times : array
            Observed (O) times of minima.
        period : float
            Period to be used to construct calculated (C) values.
        t0 : float, default: first 'min_times' value
            Epooch to be used to construct calculated (C) values.
        min_times_err : array, optional
            Error of observed (O) times of minima.

        saveplot : bool, deaful: False
            Save results.
        showplot : bool, default: False
            Show results.
        saveOC : bool, default: True
            Save constructed OC as txt file.
        filename : str, default: ''
            Beginning of txt filename.

        Returns:
        -------
        OC : array
            O-C time values.
        OCerr : array, optional
            If `min_times_err` was given, the error of OC values.
        """
        print('Calculating O-C...')
        period = float(period)

        min_times = min_times[ min_times.argsort() ]
        if min_times_err is not None:
            min_times_err = min_times_err[ min_times.argsort() ]

        OC_all = [] #List to store OC values

        if t0 is None: t0 = min_times.min()

        i=0
        for t in min_times:
            #Calculate O-C value
            OC = (t-t0)-i*period
            while True:
                if np.abs(OC)>0.9*period:
                    i=i+1
                    OC = (t-t0)-i*period
                    continue
                else:
                    break
            i=i+1
            OC_all.append( np.array([t,OC]) )

        OC_all = np.array(OC_all)
        if min_times_err is not None and saveOC:
            np.savetxt(filename+'_OC.txt',np.c_[ OC_all,min_times_err] )
        else:
            np.savetxt(filename+'_OC.txt',OC_all )

        if min_times_err is not None and saveOC:
            plt.errorbar(OC_all[:,0],OC_all[:,1],yerr=min_times_err,fmt='.')
        else:
            plt.scatter(OC_all[:,0],OC_all[:,1])
        plt.axhline(0,color='gray',zorder=0)
        plt.xlabel('Time')
        plt.ylabel('O-C (days)')
        plt.tight_layout()
        if saveplot:
            plt.savefig(filename+'_OC.pdf',format='pdf',dpi=100)
        if showplot:
            plt.show()
        plt.close('all')

        if min_times_err is not None:
            return OC_all,min_times_err
        else:
            return OC_all