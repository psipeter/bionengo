import numpy as np
import nengo
import neuron
import hyperopt
import timeit
import json
import copy
import ipdb
import os
import sys
import gc
import pickle
import matplotlib.pyplot as plt
import seaborn
from synapses import ExpSyn
from pathos.multiprocessing import ProcessingPool as Pool
from bioneuron_helper import ch_dir, make_signal, load_spikes, filter_spikes, compute_loss,\
		export_data, plot_spikes_rates, plot_hyperopt_loss

class Bahl():
	def __init__(self,P,bias):
		neuron.h.load_file('/home/pduggins/bionengo/NEURON_models/bahl.hoc')
		self.cell = neuron.h.Bahl()
		self.bias = bias
		self.bias_current = neuron.h.IClamp(self.cell.soma(0.5))
		self.bias_current.delay = 0
		self.bias_current.dur = 1e9  # TODO; limits simulation time
		self.bias_current.amp = self.bias
		self.synapses={}
		self.netcons={}
	def make_synapses(self,P,my_weights,my_locations):
		for inpt in P['inpts'].iterkeys():
			self.synapses[inpt]=np.empty((P['inpts'][inpt]['pre_neurons'],P['atrb']['n_syn']),dtype=object)
			for pre in range(P['inpts'][inpt]['pre_neurons']):
				for syn in range(P['atrb']['n_syn']):
					section=self.cell.apical(my_locations[inpt][pre][syn])
					weight=my_weights[inpt][pre][syn]
					synapse=ExpSyn(section,weight,P['atrb']['tau'])
					self.synapses[inpt][pre][syn]=synapse	
	def start_recording(self):
		self.v_record = neuron.h.Vector()
		self.v_record.record(self.cell.soma(0.5)._ref_v)
		self.ap_counter = neuron.h.APCount(self.cell.soma(0.5))
		self.t_record = neuron.h.Vector()
		self.t_record.record(neuron.h._ref_t)
		self.spikes = neuron.h.Vector()
		self.ap_counter.record(neuron.h.ref(self.spikes))
	def event_step(self,t_neuron,inpt,pre):
		for syn in self.synapses[inpt][pre]: #for each synapse in this connection
			syn.spike_in.event(t_neuron) #add a spike at time (ms)


def make_hyperopt_space(P_in,bionrn,rng):
	#adds a hyperopt-distributed weight, location, bias for each synapse for each bioneuron,
	#where each neuron is a seperate choice in hyperopt search space
	P=copy.copy(P_in)
	hyperparams={}
	hyperparams['bionrn']=bionrn
	hyperparams['bias']=hyperopt.hp.uniform('b_%s'%bionrn,P['bias_min'],P['bias_max'])
	for inpt in P['inpts'].iterkeys():
		hyperparams[inpt]={}
		for pre in range(P['inpts'][inpt]['pre_neurons']):
			hyperparams[inpt][pre]={}
			for syn in range(P['atrb']['n_syn']):
				hyperparams[inpt][pre][syn]={}
				hyperparams[inpt][pre][syn]['l']=np.round(rng.uniform(0.0,1.0),decimals=2)
				k_distance=2.0 #weight_rescale(hyperparams[inpt][pre][syn]['l'])
				k_neurons=50.0/P['inpts'][inpt]['pre_neurons']
				k_max_rates=300.0/np.average([P['inpts'][inpt]['pre_min_rate'],P['inpts'][inpt]['pre_max_rate']])
				k=k_distance*k_neurons*k_max_rates
				hyperparams[inpt][pre][syn]['w']=hyperopt.hp.uniform('w_%s_%s_%s_%s'%(bionrn,inpt,pre,syn),-k*P['w_0'],k*P['w_0'])
	P['hyperopt']=hyperparams
	return P

def load_hyperopt_space(P):
	weights={}
	locations={}
	bias=P['hyperopt']['bias']
	for inpt in P['inpts'].iterkeys():
		weights[inpt]=np.zeros((P['inpts'][inpt]['pre_neurons'],P['atrb']['n_syn']))
		locations[inpt]=np.zeros((P['inpts'][inpt]['pre_neurons'],P['atrb']['n_syn']))
		for pre in range(P['inpts'][inpt]['pre_neurons']):
			for syn in range(P['atrb']['n_syn']):
				locations[inpt][pre][syn]=P['hyperopt'][inpt][pre][syn]['l']
				weights[inpt][pre][syn]=P['hyperopt'][inpt][pre][syn]['w']
	return weights,locations,bias

def create_bioneuron(P,weights,locations,bias):
	bioneuron=Bahl(P,bias)
	bioneuron.make_synapses(P,weights,locations)
	bioneuron.start_recording()
	return bioneuron	

def run_bioneuron_event_based(P,bioneuron,all_spikes_pre):
	neuron.h.dt = P['dt_neuron']*1000
	neuron.init()
	inpts=[key for key in all_spikes_pre.iterkeys()]
	pres=[all_spikes_pre[inpt].shape[1] for inpt in inpts]
	all_input_spikes=[all_spikes_pre[inpt] for inpt in inpts]
	for time in range(all_spikes_pre[inpts[0]].shape[0]): #for each timestep
		t_neuron=time*P['dt_nengo']*1000
		for i in range(len(inpts)):  #for each input connection
			for pre in range(pres[i]): #for each input neuron
				if all_input_spikes[i][time][pre] > 0: #if input neuron spikes at time
					bioneuron.event_step(t_neuron,inpts[i],pre)
		neuron.run(time*P['dt_nengo']*1000)


'''###############################################################################################################'''
'''###############################################################################################################'''

def simulate(P):
	os.chdir(P['directory']+P['atrb']['label'])
	all_spikes_pre,all_spikes_ideal=load_spikes(P)
	spikes_ideal=all_spikes_ideal[:,P['hyperopt']['bionrn']]
	weights,locations,bias=load_hyperopt_space(P)
	bioneuron=create_bioneuron(P,weights,locations,bias)
	run_bioneuron_event_based(P,bioneuron,all_spikes_pre)
	spikes_bio,spikes_ideal,rates_bio,rates_ideal=filter_spikes(P,bioneuron,spikes_ideal)
	loss=compute_loss(P,rates_bio,rates_ideal)
	export_data(P,weights,locations,bias,spikes_bio,spikes_ideal,rates_bio,rates_ideal)
	return {'loss': loss, 'eval':P['current_eval'], 'status': hyperopt.STATUS_OK}

def run_hyperopt(P):
	#try loading hyperopt trials object from a previous run to pick up where it left off
	os.chdir(P['directory']+P['atrb']['label'])
	try:
		trials=pickle.load(open('bioneuron_%s_hyperopt_trials.p'%P['hyperopt']['bionrn'],'rb'))
		hyp_evals=np.arange(len(trials),P['atrb']['evals'])
	except IOError:
		trials=hyperopt.Trials()
		hyp_evals=range(P['atrb']['evals'])
	for t in hyp_evals:
		P['current_eval']=t
		my_seed=P['hyperopt_seed']+P['atrb']['seed']+P['hyperopt']['bionrn']*(t+1)
		best=hyperopt.fmin(simulate,
			rstate=np.random.RandomState(seed=my_seed),
			space=P,
			algo=hyperopt.tpe.suggest,
			max_evals=(t+1),
			trials=trials)
		print 'Connections into %s, bioneuron %s, hyperopt %s%%'\
			%(P['atrb']['label'],P['hyperopt']['bionrn'],100.0*(t+1)/P['atrb']['evals'])
	#find best run's directory location
	losses=[t['result']['loss'] for t in trials]
	ids=[t['result']['eval'] for t in trials]
	idx=np.argmin(losses)
	loss=np.min(losses)
	result=str(ids[idx])
	#save trials object for continued training later
	pickle.dump(trials,open('bioneuron_%s_hyperopt_trials.p'%P['hyperopt']['bionrn'],'wb'))
	#returns eval number with minimum loss for this bioneuron
	return [P['hyperopt']['bionrn'],int(result),losses]

def train_hyperparams(P):
	print 'Training connections into %s' %P['atrb']['label']
	os.chdir(P['directory']+P['atrb']['label']) #should be created in pre_build_func()
	P_list=[]
	pool = Pool(nodes=P['n_nodes'])
	rng=np.random.RandomState(seed=P['hyperopt_seed']+P['atrb']['seed'])
	for bionrn in range(P['atrb']['neurons']):
		P_hyperopt=make_hyperopt_space(P,bionrn,rng)
		# run_hyperopt(P_hyperopt)
		P_list.append(P_hyperopt)
	results=pool.map(run_hyperopt,P_list)
	#create and save a list of the eval_number associated with the minimum loss for each bioneuron
	best_hyperparam_files, rates_bio, losses = [], [], []
	for bionrn in range(len(results)):
		best_hyperparam_files.append(P['directory']+P['atrb']['label']+'/eval_%s_bioneuron_%s'%(results[bionrn][1],bionrn))
		spikes_rates_bio_ideal=np.load(best_hyperparam_files[-1]+'/spikes_rates_bio_ideal.npz')
		rates_bio.append(spikes_rates_bio_ideal['rates_bio'])
		losses.append(results[bionrn][2])
	rates_bio=np.array(rates_bio).T
	os.chdir(P['directory']+P['atrb']['label'])
	target=np.load('output_ideal_%s.npz'%P['atrb']['label'])['values']
	#plot the spikes and rates of the best run
	plot_spikes_rates(P,best_hyperparam_files,target)
	plot_hyperopt_loss(P,np.array(losses))
	np.savez('best_hyperparam_files.npz',best_hyperparam_files=best_hyperparam_files)
	return best_hyperparam_files,target,rates_bio