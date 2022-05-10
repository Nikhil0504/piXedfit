import numpy as np
from math import log10, pow, sqrt 
import sys, os
import h5py
from operator import itemgetter
from mpi4py import MPI
from astropy.io import fits
from astropy.cosmology import *

global PIXEDFIT_HOME
PIXEDFIT_HOME = os.environ['PIXEDFIT_HOME']
sys.path.insert(0, PIXEDFIT_HOME)

from piXedfit.utils.posteriors import model_leastnorm, calc_chi2, gauss_prob, gauss_prob_reduced, student_t_prob
from piXedfit.utils.filtering import interp_filters_curves, filtering_interp_filters, cwave_filters
from piXedfit.utils.redshifting import cosmo_redshifting
from piXedfit.utils.igm_absorption import igm_att_madau, igm_att_inoue


def bayesian_sedfit_gauss():
	f = h5py.File(models_spec, 'r')

	numDataPerRank = int(nmodels/size)
	idx_mpi = np.linspace(0,nmodels-1,nmodels)
	recvbuf_idx = np.empty(numDataPerRank, dtype='d')

	comm.Scatter(idx_mpi, recvbuf_idx, root=0)

	mod_params_temp = np.zeros((nparams,numDataPerRank))
	mod_fluxes_temp = np.zeros((nbands,numDataPerRank))
	mod_chi2_temp = np.zeros(numDataPerRank)
	mod_prob_temp = np.zeros(numDataPerRank)

	count = 0
	for ii in recvbuf_idx:
		# get the spectra
		wave = f['mod/spec/wave'][:]
		str_temp = 'mod/spec/f%d' % int(ii)
		extnc_spec = f[str_temp][:]

		# redshifting
		redsh_wave,redsh_spec = cosmo_redshifting(DL_Gpc=DL_Gpc,cosmo=cosmo,H0=H0,Om0=Om0,
													z=gal_z,wave=wave,spec=extnc_spec)

		# IGM absorption:
		if add_igm_absorption == 1:
			if igm_type == 0 or igm_type == 'madau1995':
				trans = igm_att_madau(redsh_wave,gal_z)
				redsh_spec = redsh_spec*trans
			elif igm_type == 1 or igm_type == 'inoue2014':
				trans = igm_att_inoue(redsh_wave,gal_z)
				redsh_spec = redsh_spec*trans

		# filtering
		fluxes = filtering_interp_filters(redsh_wave,redsh_spec,interp_filters_waves,interp_filters_trans)
		norm = model_leastnorm(obs_fluxes,obs_flux_err,fluxes)
		norm_fluxes = norm*fluxes

		# calculate chi-square and prob
		chi2 = calc_chi2(obs_fluxes,obs_flux_err,norm_fluxes)

		if gauss_likelihood_form == 0:
			prob0 = gauss_prob(obs_fluxes,obs_flux_err,norm_fluxes)
		elif gauss_likelihood_form == 1:
			prob0 = gauss_prob_reduced(obs_fluxes,obs_flux_err,norm_fluxes)

		mod_chi2_temp[int(count)] = chi2
		mod_prob_temp[int(count)] = prob0
		mod_fluxes_temp[:,int(count)] = fluxes   # before normalized

		# get parameters
		for pp in range(0,nparams):
			str_temp = 'mod/par/%s' % params[pp]
			if params[pp]=='log_mass' or params[pp]=='log_sfr' or params[pp]=='log_dustmass':
				mod_params_temp[pp][int(count)] = f[str_temp][int(ii)] + log10(norm)
			else:
				mod_params_temp[pp][int(count)] = f[str_temp][int(ii)]

		count = count + 1

		sys.stdout.write('\r')
		sys.stdout.write('rank: %d  Calculation process: %d from %d  --->  %d%%' % (rank,count,len(recvbuf_idx),count*100/len(recvbuf_idx)))
		sys.stdout.flush()
	sys.stdout.write('\n')

	mod_params = np.zeros((nparams,nmodels))
	mod_fluxes = np.zeros((nbands,nmodels))
	mod_chi2 = np.zeros(nmodels)
	mod_prob = np.zeros(nmodels)
				
	# gather the scattered data and collect to rank=0
	comm.Gather(mod_chi2_temp, mod_chi2, root=0)
	comm.Gather(mod_prob_temp, mod_prob, root=0)
	for pp in range(0,nparams):
		comm.Gather(mod_params_temp[pp], mod_params[pp], root=0)
	for bb in range(0,nbands):
		comm.Gather(mod_fluxes_temp[bb], mod_fluxes[bb], root=0)

	status_add_err = np.zeros(1)
	modif_obs_flux_err = np.zeros(nbands)

	if rank == 0:
		idx0, min_val = min(enumerate(mod_chi2), key=itemgetter(1))

		fluxes = mod_fluxes[:,idx0]

		#print ("reduced chi2 value of the best-fitting model: %lf" % (mod_chi2[idx0]/nbands))
		if mod_chi2[idx0]/nbands > redcd_chi2:  
			sys_err_frac = 0.01
			while sys_err_frac <= 0.5:
				modif_obs_flux_err = np.sqrt(np.square(obs_flux_err) + np.square(sys_err_frac*obs_fluxes))
				chi2 = calc_modchi2_leastnorm(obs_fluxes,modif_obs_flux_err,fluxes)
				if chi2/nbands <= redcd_chi2:
					break
				sys_err_frac = sys_err_frac + 0.01
			#print ("After adding %lf fraction to systematic error, reduced chi2 of best-fit model becomes: %lf" % (sys_err_frac,chi2/nbands))
			status_add_err[0] = 1
		elif mod_chi2[idx0]/nbands <= redcd_chi2:
			status_add_err[0] = 0

	comm.Bcast(status_add_err, root=0)

	if status_add_err[0] == 0:
		# Broadcast
		comm.Bcast(mod_params, root=0)
		comm.Bcast(mod_fluxes, root=0)
		comm.Bcast(mod_chi2, root=0)
		comm.Bcast(mod_prob, root=0)
		 
	elif status_add_err[0] == 1:
		comm.Bcast(mod_fluxes, root=0)
		comm.Bcast(modif_obs_flux_err, root=0) 

		idx_mpi = np.linspace(0,nmodels-1,nmodels)
		recvbuf_idx = np.empty(numDataPerRank, dtype='d')

		comm.Scatter(idx_mpi, recvbuf_idx, root=0)

		mod_chi2_temp = np.zeros(numDataPerRank)
		mod_prob_temp = np.zeros(numDataPerRank)
		mod_params_temp = np.zeros((nparams,numDataPerRank))

		count = 0
		for ii in recvbuf_idx:
			fluxes = mod_fluxes[:,int(ii)]

			norm = model_leastnorm(obs_fluxes,modif_obs_flux_err,fluxes)
			norm_fluxes = norm*fluxes

			# calculate chi-square and prob
			chi2 = calc_chi2(obs_fluxes,modif_obs_flux_err,norm_fluxes)

			if gauss_likelihood_form == 0:
				prob0 = gauss_prob(obs_fluxes,modif_obs_flux_err,norm_fluxes)
			elif gauss_likelihood_form == 1:
				prob0 = gauss_prob_reduced(obs_fluxes,modif_obs_flux_err,norm_fluxes)

			mod_chi2_temp[int(count)] = chi2
			mod_prob_temp[int(count)] = prob0

			# get parameters
			for pp in range(0,nparams):
				str_temp = 'mod/par/%s' % params[pp]
				if params[pp]=='log_mass' or params[pp]=='log_sfr' or params[pp]=='log_dustmass':
					mod_params_temp[pp][int(count)] = f[str_temp][int(ii)] + log10(norm)
				else:
					mod_params_temp[pp][int(count)] = f[str_temp][int(ii)]

			count = count + 1

		mod_prob = np.zeros(nmodels)
		mod_chi2 = np.zeros(nmodels)
		mod_params = np.zeros(nparams,nmodels)
				
		# gather the scattered data
		comm.Gather(mod_chi2_temp, mod_chi2, root=0)
		comm.Gather(mod_prob_temp, mod_prob, root=0)
		for pp in range(0,nparams):
			comm.Gather(mod_params_temp[pp], mod_params[pp], root=0)

		# Broadcast
		comm.Bcast(mod_chi2, root=0)
		comm.Bcast(mod_prob, root=0)
		comm.Bcast(mod_params, root=0)

		## end of status_add_err[0] == 1

	f.close()

	return mod_params, mod_chi2, mod_prob, modif_obs_flux_err


def bayesian_sedfit_student_t():
	f = h5py.File(models_spec, 'r')

	numDataPerRank = int(nmodels/size)
	idx_mpi = np.linspace(0,nmodels-1,nmodels)
	recvbuf_idx = np.empty(numDataPerRank, dtype='d')

	comm.Scatter(idx_mpi, recvbuf_idx, root=0)

	mod_params_temp = np.zeros((nparams,numDataPerRank))
	mod_fluxes_temp = np.zeros((nbands,numDataPerRank))
	mod_chi2_temp = np.zeros(numDataPerRank)
	mod_prob_temp = np.zeros(numDataPerRank)

	count = 0
	for ii in recvbuf_idx:
		# get the spectra
		wave = f['mod/spec/wave'][:]
		str_temp = 'mod/spec/f%d' % int(ii)
		extnc_spec = f[str_temp][:]

		# redshifting
		redsh_wave,redsh_spec = cosmo_redshifting(DL_Gpc=DL_Gpc,cosmo=cosmo,H0=H0,Om0=Om0,
													z=gal_z,wave=wave,spec=extnc_spec)

		# IGM absorption:
		if add_igm_absorption == 1:
			if igm_type == 0 or igm_type == 'madau1995':
				trans = igm_att_madau(redsh_wave,gal_z)
				redsh_spec = redsh_spec*trans
			elif igm_type == 1 or igm_type == 'inoue2014':
				trans = igm_att_inoue(redsh_wave,gal_z)
				redsh_spec = redsh_spec*trans

		# filtering
		fluxes = filtering_interp_filters(redsh_wave,redsh_spec,interp_filters_waves,interp_filters_trans)
		norm = model_leastnorm(obs_fluxes,obs_flux_err,fluxes)
		norm_fluxes = norm*fluxes

		# calculate chi-square and prob.
		chi2 = calc_chi2(obs_fluxes,obs_flux_err,norm_fluxes)
		chi = (obs_fluxes-norm_fluxes)/obs_flux_err
		prob0 = student_t_prob(dof,chi)

		mod_chi2_temp[int(count)] = chi2
		mod_prob_temp[int(count)] = prob0
		mod_fluxes_temp[:,int(count)] = fluxes    # before normalized

		# get parameters
		for pp in range(0,nparams):
			str_temp = 'mod/par/%s' % params[pp]
			if params[pp]=='log_mass' or params[pp]=='log_sfr' or params[pp]=='log_dustmass':
				mod_params_temp[pp][int(count)] = f[str_temp][int(ii)] + log10(norm)
			else:
				mod_params_temp[pp][int(count)] = f[str_temp][int(ii)]

		count = count + 1

		sys.stdout.write('\r')
		sys.stdout.write('rank: %d  Calculation process: %d from %d  --->  %d%%' % (rank,count,len(recvbuf_idx),count*100/len(recvbuf_idx)))
		sys.stdout.flush()
	sys.stdout.write('\n')

	mod_params = np.zeros((nparams,nmodels))
	mod_fluxes = np.zeros((nbands,nmodels))
	mod_chi2 = np.zeros(nmodels)
	mod_prob = np.zeros(nmodels)
				
	# gather the scattered data and collect to rank=0
	comm.Gather(mod_chi2_temp, mod_chi2, root=0)
	comm.Gather(mod_prob_temp, mod_prob, root=0)
	for pp in range(0,nparams):
		comm.Gather(mod_params_temp[pp], mod_params[pp], root=0)
	for bb in range(0,nbands):
		comm.Gather(mod_fluxes_temp[bb], mod_fluxes[bb], root=0)

	status_add_err = np.zeros(1)
	modif_obs_flux_err = np.zeros(nbands)

	if rank == 0:
		idx0, min_val = min(enumerate(mod_chi2), key=itemgetter(1))

		fluxes = mod_fluxes[:,idx0]

		#print ("reduced chi2 value of the best-fitting model: %lf" % (mod_chi2[idx0]/nbands))
		if mod_chi2[idx0]/nbands > redcd_chi2:  
			sys_err_frac = 0.01
			while sys_err_frac <= 0.5:
				modif_obs_flux_err = np.sqrt(np.square(obs_flux_err) + np.square(sys_err_frac*obs_fluxes))
				chi2 = calc_modchi2_leastnorm(obs_fluxes,modif_obs_flux_err,fluxes)
				if chi2/nbands <= redcd_chi2:
					break
				sys_err_frac = sys_err_frac + 0.01
			#print ("After adding %lf fraction to systematic error, reduced chi2 of best-fit model becomes: %lf" % (sys_err_frac,chi2/nbands))
			status_add_err[0] = 1
		elif mod_chi2[idx0]/nbands <= redcd_chi2:
			status_add_err[0] = 0

	comm.Bcast(status_add_err, root=0)

	if status_add_err[0] == 0:
		# Broadcast
		comm.Bcast(mod_params, root=0)
		comm.Bcast(mod_fluxes, root=0)
		comm.Bcast(mod_chi2, root=0)
		comm.Bcast(mod_prob, root=0)
		 
	elif status_add_err[0] == 1:
		comm.Bcast(mod_fluxes, root=0)
		comm.Bcast(modif_obs_flux_err, root=0) 

		idx_mpi = np.linspace(0,nmodels-1,nmodels)
		recvbuf_idx = np.empty(numDataPerRank, dtype='d')

		comm.Scatter(idx_mpi, recvbuf_idx, root=0)

		mod_chi2_temp = np.zeros(numDataPerRank)
		mod_prob_temp = np.zeros(numDataPerRank)
		mod_params_temp = np.zeros((nparams,numDataPerRank))

		count = 0
		for ii in recvbuf_idx:
			fluxes = mod_fluxes[:,int(ii)]
			norm = model_leastnorm(obs_fluxes,modif_obs_flux_err,fluxes)
			norm_fluxes = norm*fluxes

			# calculate model's chi2 and prob.
			chi2 = calc_chi2(obs_fluxes,modif_obs_flux_err,norm_fluxes)
			chi = (obs_fluxes-norm_fluxes)/modif_obs_flux_err
			prob0 = student_t_prob(dof,chi)

			mod_chi2_temp[int(count)] = chi2
			mod_prob_temp[int(count)] = prob0

			# get parameters
			for pp in range(0,nparams):
				str_temp = 'mod/par/%s' % params[pp]
				if params[pp]=='log_mass' or params[pp]=='log_sfr' or params[pp]=='log_dustmass':
					mod_params_temp[pp][int(count)] = f[str_temp][int(ii)] + log10(norm)
				else:
					mod_params_temp[pp][int(count)] = f[str_temp][int(ii)]

			count = count + 1

		mod_prob = np.zeros(nmodels)
		mod_chi2 = np.zeros(nmodels)
		mod_params = np.zeros(nparams,nmodels)
				
		# gather the scattered data
		comm.Gather(mod_chi2_temp, mod_chi2, root=0)
		comm.Gather(mod_prob_temp, mod_prob, root=0)
		for pp in range(0,nparams):
			comm.Gather(mod_params_temp[pp], mod_params[pp], root=0)

		# Broadcast
		comm.Bcast(mod_chi2, root=0)
		comm.Bcast(mod_prob, root=0)
		comm.Bcast(mod_params, root=0)

		## end of status_add_err[0] == 1

	f.close()

	return mod_params, mod_chi2, mod_prob, modif_obs_flux_err


def store_to_fits(sampler_params,mod_chi2,mod_prob,fits_name_out):

	idx, min_val = min(enumerate(mod_chi2), key=itemgetter(1))
	bfit_chi2 = mod_chi2[idx]

	f = h5py.File(models_spec, 'r')
	# best-fit SED
	wave = f['mod/spec/wave'][:]
	str_temp = 'mod/spec/f%d' % idx
	extnc_spec = f[str_temp][:]
	redsh_wave,redsh_spec = cosmo_redshifting(DL_Gpc=DL_Gpc,cosmo=cosmo,H0=H0,Om0=Om0,
													z=gal_z,wave=wave,spec=extnc_spec)
	if add_igm_absorption == 1:
		if igm_type == 0 or igm_type == 'madau1995':
			trans = igm_att_madau(redsh_wave,gal_z)
			redsh_spec = redsh_spec*trans
		elif igm_type == 1 or igm_type == 'inoue2014':
			trans = igm_att_inoue(redsh_wave,gal_z)
			redsh_spec = redsh_spec*trans
	fluxes = filtering_interp_filters(redsh_wave,redsh_spec,interp_filters_waves,interp_filters_trans)
	norm = model_leastnorm(obs_fluxes,obs_flux_err,fluxes)
	mod_fluxes = norm*fluxes
	redsh_spec = norm*redsh_spec
	f.close()

	#==> Get median likelihood parameters
	crit_chi2 = np.percentile(mod_chi2, perc_chi2)
	idx_sel = np.where((mod_chi2<=crit_chi2) & (sampler_params['log_sfr']>-29.0) & (np.isnan(mod_prob)==False) & (np.isinf(mod_prob)==False) & (mod_prob>0))

	array_prob = mod_prob[idx_sel[0]]
	tot_prob = np.sum(array_prob)

	params_bfits = np.zeros((nparams,2))
	for pp in range(0,nparams):
		array_val = sampler_params[params[pp]][idx_sel[0]]

		mean_val = np.sum(array_val*array_prob)/tot_prob
		mean_val2 = np.sum(np.square(array_val)*array_prob)/tot_prob
		std_val = sqrt(abs(mean_val2 - (mean_val**2)))

		params_bfits[pp][0] = mean_val
		params_bfits[pp][1] = std_val

	# store to FITS file
	hdr = fits.Header()
	hdr['imf'] = imf
	hdr['nparams'] = nparams
	hdr['sfh_form'] = sfh_form
	hdr['dust_ext_law'] = dust_ext_law
	hdr['nfilters'] = nbands
	hdr['duste_stat'] = duste_switch
	if duste_switch=='duste' or duste_switch==1:
		if fix_dust_index == 1:
			hdr['dust_index'] = fix_dust_index_val
	hdr['add_neb_emission'] = add_neb_emission
	if add_neb_emission == 1:
		hdr['gas_logu'] = gas_logu
	hdr['add_agn'] = add_agn
	hdr['add_igm_absorption'] = add_igm_absorption
	hdr['likelihood_form'] = likelihood_form
	if likelihood_form == 'student_t':
		hdr['dof'] = dof
	if add_igm_absorption == 1:
		hdr['igm_type'] = igm_type
	for bb in range(0,nbands):
		str_temp = 'fil%d' % bb
		hdr[str_temp] = filters[bb]
		str_temp = 'flux%d' % bb
		hdr[str_temp] = obs_fluxes[bb]
		str_temp = 'flux_err%d' % bb 
		hdr[str_temp] = obs_flux_err[bb]
	hdr['gal_z'] = gal_z
	hdr['free_z'] = 0
	hdr['cosmo'] = cosmo
	hdr['H0'] = H0
	hdr['Om0'] = Om0

	# parameters
	for pp in range(0,nparams):
		str_temp = 'param%d' % pp
		hdr[str_temp] = params[pp]

	# chi-square
	hdr['redcd_chi2'] = bfit_chi2/nbands
	hdr['perc_chi2'] = perc_chi2

	# add columns
	cols0 = []
	col_count = 1
	str_temp = 'col%d' % col_count
	hdr[str_temp] = 'rows'
	col = fits.Column(name='rows', format='4A', array=['mean','std'])
	cols0.append(col)

	#=> parameters
	for pp in range(0,nparams):
		col_count = col_count + 1
		str_temp = 'col%d' % col_count
		hdr[str_temp] = params[pp]
		col = fits.Column(name=params[pp], format='D', array=np.array([params_bfits[pp][0],params_bfits[pp][1]]))
		cols0.append(col)

	hdr['ncols'] = col_count

	# combine header
	primary_hdu = fits.PrimaryHDU(header=hdr)

	# combine binary table HDU1: parameters derived from fitting rdsps
	cols = fits.ColDefs(cols0)
	hdu = fits.BinTableHDU.from_columns(cols, name='fit_params')

	#==> add table for parameters that have minimum chi-square
	cols0 = []
	for pp in range(0,nparams):
		col = fits.Column(name=params[pp], format='D', array=np.array([sampler_params[params[pp]][idx]]))
		cols0.append(col)
	cols = fits.ColDefs(cols0)
	hdu1 = fits.BinTableHDU.from_columns(cols, name='minchi2_params')

	#==> make new table for best-fit spectra: redsh_wave,redsh_spec
	cols0 = []
	col = fits.Column(name='spec_wave', format='D', array=np.array(redsh_wave))
	cols0.append(col)
	col = fits.Column(name='spec_flux', format='D', array=np.array(redsh_spec))
	cols0.append(col)
	cols = fits.ColDefs(cols0)
	hdu2 = fits.BinTableHDU.from_columns(cols, name='bfit_spec')

	#==> make new table for best-fit photometry
	photo_cwave = cwave_filters(filters)

	cols0 = []
	col = fits.Column(name='photo_wave', format='D', array=np.array(photo_cwave))
	cols0.append(col)
	col = fits.Column(name='photo_flux', format='D', array=np.array(mod_fluxes))
	cols0.append(col)
	cols = fits.ColDefs(cols0)
	hdu3 = fits.BinTableHDU.from_columns(cols, name='bfit_photo')

	# combine all
	hdul = fits.HDUList([primary_hdu, hdu, hdu1, hdu2, hdu3])
	hdul.writeto(fits_name_out, overwrite=True)


"""
USAGE: mpirun -np [npros] python ./rdsps_pcmod.py (1)name_filters_list (2)name_config (3)name_SED_txt (4)name_out_fits
"""

temp_dir = PIXEDFIT_HOME+'/data/temp/'

global comm, size, rank
comm = MPI.COMM_WORLD
size = comm.Get_size() 
rank = comm.Get_rank() 

# configuration file
config_file = str(sys.argv[2])
data = np.genfromtxt(temp_dir+config_file, dtype=str)
config_data = {}
for ii in range(0,len(data[:,0])):
	str_temp = data[:,0][ii]
	config_data[str_temp] = data[:,1][ii]

# filters
global filters, nbands
name_filters = str(sys.argv[1])
filters = np.genfromtxt(temp_dir+name_filters, dtype=str)
nbands = len(filters)

global likelihood_form
likelihood_form = config_data['likelihood']

# degree of freedom in the student's t likelihood function, only relevant if likelihood_form='student_t'
global dof
dof = float(config_data['dof'])

global gauss_likelihood_form
gauss_likelihood_form = int(config_data['gauss_likelihood_form'])

# get input SEDs
inputSED_txt = str(sys.argv[3])
f = h5py.File(inputSED_txt, 'r')
n_obs_sed = int(f['obs_seds'].attrs['nbins_calc'])
bulk_obs_fluxes = np.zeros((n_obs_sed,nbands))
bulk_obs_flux_err = np.zeros((n_obs_sed,nbands))
for ii in range(0,n_obs_sed):
	str_temp = 'obs_seds/flux/b%d_f' % ii 
	bulk_obs_fluxes[ii] = f[str_temp][:]

	str_temp = 'obs_seds/flux_err/b%d_ferr' % ii 
	bulk_obs_flux_err[ii] = f[str_temp][:]
f.close()

# add systematic error accommodating various factors, including modeling uncertainty, assume systematic error of 0.1
sys_err_frac = 0.1
bulk_obs_flux_err = np.sqrt(np.square(bulk_obs_flux_err) + np.square(sys_err_frac*bulk_obs_fluxes))

# names of output FITS files
name_outs = str(sys.argv[4])
name_out_fits = np.genfromtxt(temp_dir+name_outs, dtype=str)

global perc_chi2
perc_chi2 = float(config_data['perc_chi2'])

# cosmology
global cosmo, H0, Om0
cosmo = int(config_data['cosmo'])
H0 = float(config_data['H0'])
Om0 = float(config_data['Om0'])

# redshift
global gal_z, DL_Gpc
gal_z = float(config_data['gal_z'])
# luminosity distance
if cosmo=='flat_LCDM' or cosmo==0:
	cosmo1 = FlatLambdaCDM(H0=H0, Om0=Om0)
	DL_Gpc0 = cosmo1.luminosity_distance(gal_z)      # in unit of Mpc
elif cosmo=='WMAP5' or cosmo==1:
	DL_Gpc0 = WMAP5.luminosity_distance(gal_z)
elif cosmo=='WMAP7' or cosmo==2:
	DL_Gpc0 = WMAP7.luminosity_distance(gal_z)
elif cosmo=='WMAP9' or cosmo==3:
	DL_Gpc0 = WMAP9.luminosity_distance(gal_z)
elif cosmo=='Planck13' or cosmo==4:
	DL_Gpc0 = Planck13.luminosity_distance(gal_z)
elif cosmo=='Planck15' or cosmo==5:
	DL_Gpc0 = Planck15.luminosity_distance(gal_z)
#elif cosmo=='Planck18' or cosmo==6:
#	DL_Gpc0 = Planck18.luminosity_distance(gl_z)
DL_Gpc = DL_Gpc0.value/1.0e+3

# HDF5 file containing pre-calculated model SEDs
global models_spec
models_spec = config_data['models_spec']

# data of pre-calculated model SEDs
f = h5py.File(models_spec, 'r')

# number of model SEDs
global nmodels
nmodels = int(f['mod'].attrs['nmodels']/size)*size

# get list of parameters
global nparams, params
nparams = int(f['mod'].attrs['nparams_all'])  # include all possible parameters
params = []
for pp in range(0,nparams):
	str_temp = 'par%d' % pp 
	params.append(f['mod'].attrs[str_temp])

# modeling configurations
global imf, sfh_form, dust_ext_law, duste_switch, add_neb_emission, add_agn, gas_logu, fix_dust_index, fix_dust_index_val
imf = f['mod'].attrs['imf_type']
sfh_form = f['mod'].attrs['sfh_form']
dust_ext_law = f['mod'].attrs['dust_ext_law']
duste_switch = f['mod'].attrs['duste_switch']
add_neb_emission = f['mod'].attrs['add_neb_emission']
add_agn = f['mod'].attrs['add_agn']
gas_logu = f['mod'].attrs['gas_logu']
if duste_switch=='duste' or duste_switch==1:
	if 'dust_index' in params:
		fix_dust_index = 0 
	else:
		fix_dust_index = 1 
		fix_dust_index_val = f['mod'].attrs['dust_index']
f.close()

# igm absorption
global add_igm_absorption,igm_type
add_igm_absorption = int(config_data['add_igm_absorption'])
igm_type = int(config_data['igm_type'])

global interp_filters_waves, interp_filters_trans
interp_filters_waves,interp_filters_trans = interp_filters_curves(filters)

# running the calculation
global redcd_chi2
redcd_chi2 = 2.0

global obs_fluxes, obs_flux_err

# iteration for each SED
for ii in range(0,n_obs_sed):
	obs_fluxes = bulk_obs_fluxes[ii]
	obs_flux_err = bulk_obs_flux_err[ii]

	# running the calculation
	if likelihood_form == 'gauss':
		mod_params, mod_chi2, mod_prob, modif_obs_flux_err = bayesian_sedfit_gauss()
	elif likelihood_form == 'student_t':
		mod_params, mod_chi2, mod_prob, modif_obs_flux_err = bayesian_sedfit_student_t()
	else:
		print ("likelihood_form is not recognized!")
		sys.exit()

	if np.sum(modif_obs_flux_err) != 0:
		obs_flux_err = modif_obs_flux_err

	# change the format to dictionary
	sampler_params = {}
	for pp in range(0,nparams):
		sampler_params[params[pp]] = mod_params[pp]

	# store to fits file
	if rank == 0:
		fits_name_out = name_out_fits[ii]
		store_to_fits(sampler_params,mod_chi2,mod_prob,fits_name_out)

