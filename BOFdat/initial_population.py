"""
Initial population
======

This module generates initial population for the genetic algorithm.

"""
import random
from random import shuffle, randint
# import cobra
import pandas as pd
import numpy as np
from itertools import repeat
from deap import creator, base, tools
from deap.tools import History, HallOfFame
from cobra import Reaction
from cobra.flux_analysis import single_gene_deletion
from cobra.util.solver import linear_reaction_coefficients
from sklearn.metrics import matthews_corrcoef
from scipy.spatial.distance import hamming
# Recording data and stats
import matplotlib.pyplot as plt
import networkx
# Parallel
import multiprocessing
# Timeout imports and definitions
from concurrent.futures import TimeoutError
from pebble import ProcessPool, ProcessExpired


def _get_biomass_objective_function(model):
    from cobra.util.solver import linear_reaction_coefficients
    return list(linear_reaction_coefficients(model).keys())[0]

def _assess_solvability(m, model):
    # Identify the list of metabolites that do not prevent the model to solve when added to the BOF
    biomass = _get_biomass_objective_function(model)
    biomass.remove_from_model()
    BIOMASS = Reaction('BIOMASS')
    model.add_reactions([BIOMASS])
    model.reactions.BIOMASS.add_metabolites({m: -1.})
    model.reactions.BIOMASS.objective_coefficient = 1.
    solution = model.optimize()
    # If the model can produce that metabolite
    if solution.f > 1e-9:
        return (m,True)
    else:
        return (m,False)

def _branching_analysis(model):
    metab, number_of_rxn = [], []
    for m in model.metabolites:
        metab.append(m.id)
        number_of_rxn.append(len(m.reactions))
        branching_df = pd.DataFrame({'Metab': metab, 'Number of metab': number_of_rxn})
        # Define threshold using inner stats about the data
        THRESHOLD = branching_df['Number of metab'].median() + branching_df['Number of metab'].std()
        branching_df = branching_df[branching_df['Number of metab'] > THRESHOLD]
        branching_df.sort_values('Number of metab', inplace=True, ascending=False)

        return [m for m in branching_df['Metab']]

def _make_ind(m,metab_index):
    # Generates an individual with  metabolites
    ind_dict = {}
    for i in metab_index:
        if i.id == m.id:
            ind_dict[i.id] = 1
        else:
            ind_dict[i.id] = 0
    return ind_dict

def _eval_ind(individual, model, exp_ess, distance):
    # Set this as warning
    model.solver = 'gurobi'
    old_biomass = list(linear_reaction_coefficients(model).keys())[0]  # index removed
    old_biomass.remove_from_model()
    # Make a biomass reaction and optimize for it
    biomass = Reaction('BIOMASS')
    model.add_reaction(biomass)
    biomass.add_metabolites({model.metabolites.get_by_id(individual[individual == 1].name): -0.1})
    biomass.objective_coefficient = 1.

    # Generate deletion results --> BOTTLENECK FOR SURE
    deletion_results = single_gene_deletion(model, model.genes,processes=8)

    # Filter the results to get a boolean result
    a = [(str(next(iter(i))), 1) for i in deletion_results[deletion_results['growth'] > 1e-3].index]
    b = [(str(next(iter(i))), 0) for i in deletion_results[deletion_results['growth'] <= 1e-3].index]
    c = a + b
    pred_ess = pd.DataFrame(c, columns=['Genes', 'Predicted_growth'])
    compare_df = pd.merge(right=exp_ess, left=pred_ess, on='Genes', how='inner')

    # Apply hamming distance
    u = np.array([f for f in compare_df.Measured_growth])
    v = np.array([x for x in compare_df.Predicted_growth])
    if distance == 'hd':
        dist = hamming(u, v)
        return dist
    elif distance == 'mcc':
        dist = matthews_corrcoef(u, v)
        return dist
    else:
        print('Error: Invalid distance metric')

def _pebble_map(metabolite_list,model):
    processes = multiprocessing.cpu_count()
    model_iter = repeat(model)
    with ProcessPool(processes) as pool:
        future = pool.map(_assess_solvability, metabolite_list,model_iter, timeout=40)
        iterator = future.result()
        all_results = []
        while True:
            try:
                result = next(iterator)
                all_results.append(result)
            except StopIteration:
                break
            except TimeoutError as error:
                print("function took longer than %d seconds" % error.args[1])
                result = 0, 100
                all_results.append(result)
            except ProcessExpired as error:
                print("%s. Exit code: %d" % (error, error.exitcode))
            except Exception as error:
                print("function raised %s" % error)
                print(error.traceback)  # Python's traceback of remote process

    return all_results


def _generate_metab_index(model, base_biomass,exp_essentiality):
    metab_index = [m for m in model.metabolites]
    # 1- Remove metabolites present in the base biomass
    base_biomass = dict(zip([model.metabolites.get_by_id(m) for m in base_biomass.Metabolites],
                            [coeff for coeff in base_biomass.Coefficients]))
    base_biomass_metab = [k.id for k in base_biomass.keys()]
    metab_index = [m for m in metab_index if m.id not in base_biomass_metab]
    # 2- Remove highly branched metabolites
    highly_branched_metab = _branching_analysis(model)
    metab_index = [m for m in metab_index if m.id not in highly_branched_metab]
    #3- Remove metabolites from atp hydrolysis reaction
    atp_hydrolysis = ['atp', 'h2o', 'adp', 'pi', 'h', 'ppi']
    metab_index = [m for m in metab_index if m.id not in atp_hydrolysis]
    # 3- Remove unsolvable metabolites
    solvability = _pebble_map(metab_index, model)
    metab_index = [t[0] for t in solvability if t[1] == True]

    # 4- Find the most relevant metabolites for a maximum gene essentiality prediction
    #Generate a population to test mcc of each metabolite one by one
    pop_list = []
    for m in metab_index:
        pop_list.append(_make_ind(m, metab_index))
    pop_df = pd.DataFrame(pop_list, index=[m.id for m in metab_index])
    print(pop_df)
    metab, mcc = [], []
    exp_ess = pd.read_csv(exp_essentiality, index_col=0)
    for col in pop_df:
        metab.append(pop_df[col][pop_df[col] == 1].name)
        mcc.append(_eval_ind(pop_df[col], model, exp_ess, 'mcc'))

    result_df = pd.DataFrame({'metab': metab, 'mcc': mcc})
    result_df.sort_values('mcc', ascending=False, inplace=True)
    THRESHOLD = result_df['mcc'].std() + result_df['mcc'].median()

    return [m for m in result_df['metab'][result_df['mcc'] > THRESHOLD]]



def _generate_initial_populations(population_name, metab_index, base_biomass):
    pop_size = float(metab_index)/20
    #5- Generate appropriate number of initial population to obtain a significant result after running the genetic algorithm
    # Save the index before modification madness
    df_index = [m.id for m in metab_index]

    number_of_cores = multiprocessing.cpu_count()
    biomass_list = []
    it = 1
    while len(biomass_list) < pop_size:
        print('Im in loop %s and I have %s valid individuals' % (it, len(biomass_list)))
        ind_list = []
        for n in range(number_of_cores):
            # Generate an ordered index with any metabolites but those present in the base biomass
            # index = [m for m in metab_list if m.id not in [v.id for v in base_biomass.keys()]]
            # Generate an initial population from any metabolite
            index = [m for m in metab_list]
            shuffle(index)
            # Make the individual
            ind_dict = make_ind(index)
            biomass = {}
            biomass.update(base_biomass)
            for i in index:
                # Add metabolites to the temporary biomass
                if ind_dict.get(i) == 1:
                    biomass.update({i: 0.1})
            # Change the signs everywhere
            biomass = {k: -abs(v) for k, v in biomass.items()}
            biomass_name = 'biomass' + str(it) + '_' + str(n)
            ind_list.append({biomass_name: biomass})

        solutions = pebble_map(eval_func, ind_list)
        for sol in solutions:
            if sol != 'unsolvable':
                biomass_list.append(sol)

        shuffle(metab_list)
        it += 1

    # Convert biomass list into binary individual
    df = pd.DataFrame(index=df_index)
    for b in biomass_list:
        ind = []
        for m in df_index:
            if m in [d.id for d in list(b.values())[0]]:
                ind.append(1)
            else:
                ind.append(0)
        df[list(b.keys())[0]] = ind

    # Write the initial population to file
    df.to_csv(population_name)


def make_initial_population(population_name, model, base_biomass, exp_essentiality,number_of_populations=3):
    """
    This function generates the initial population to run the genetic algorithm on.

    :param population_name: The name and path to write the populations to
    :param pop_size: The number of populations to be generated, default=3
    :param model: Model object
    :param base_biomass: The output of step 1 and 2 of BOFdat
    :param exp_essentiality: Experimental essentiality as a 2 columns csv file the output of the
    :return:
    """
    # Convert base_biomass dataframe to dictionary
    # base_biomass = dict(zip([model.metabolites.get_by_id(k) for k in base_biomass['Metabolites']],
    #                   [v for v in base_biomass['Coefficients']]))
    #1- Make the metabolite index
    metab_index = _generate_metab_index(model, base_biomass, exp_essentiality)
    #2- Make the initial populations
    for n in range(number_of_populations):
        pop_name = population_name + '_' + str(n) + '.csv'
        _generate_initial_populations(pop_name, metab_index, base_biomass)
