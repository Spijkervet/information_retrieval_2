# -*- coding: utf-8 -*

import os
import json
import sys
import time
import argparse
import torch
import torch.nn.functional as F
import numpy as np
import models as srnn
from sampler import get_dataloader

USER_SIZE = None
ITEM_SIZE = None
HIDDEN_SIZE = 40
LEARNING_RATE = 0.001
TOP = 10  # top 10 statistics nDCG@10, HIT@10
BEST_NDCG = 0
OUTPUT_PATH = None
OUTPUT_FILE = None

DATAFILE = None
MODEL_FILE = None
PREDICTIONS_FILE = None
OPTIM_FILE = None
MODEL_DIR = '../models/'
DATANAME = None

ITEM_TRAIN = {}
ITEM_TEST = {}
WEEKDAY_TRAIN = {}
WEEKDAY_TEST = {}
HOUR_TRAIN = {}
HOUR_TEST = {}
INTERVAL_TRAIN = {}
INTERVAL_TEST = {}

# Context Sizes
WEEKDAY_SIZE = None
HOUR_SIZE = None
INTERVAL_SIZE = None

BPR_LOSS = 'BPR_LOSS'
BPR_LOSS_R = 'BPR_LOSS_R'

RUN_CUDA = False
LONG_STR = 'Long'
FLOAT_STR = 'Float'

# Log of indexes(used for nDCG)
LOG_OF_INDEXES = None


def pre_data(sequence_length=-1, split=0.8):
    global ITEM_TRAIN
    global ITEM_TEST
    global DATAFILE

    all_cart = []
    data = open(DATAFILE, 'r')
    lines = data.readlines()
    for line in lines:
        line1 = json.loads(line)
        all_cart.append(line1)

    # loop over all sequences
    for i in range(len(all_cart)):
        item_train = []
        item_test = []
        weekday_train = []
        weekday_test = []
        hour_train = []
        hour_test = []
        interval_train = []
        interval_test = []

        behavior_list = all_cart[i]

        # ORIGINAL
        if split != 1:
            behavior_train = behavior_list[0:int(split * len(behavior_list))]
            behavior_test = behavior_list[int(split * len(behavior_list)):]
        else:
        # use the final item as test item just like in SASRec
            behavior_train = behavior_list[:-1]
            behavior_test = behavior_list[-1:]

        for behavior in behavior_train:
            item_train.append(behavior[0])
            weekday_train.append(behavior[1])
            hour_train.append(behavior[2])
            interval_train.append(behavior[3])

        for behavior in behavior_test:
            item_test.append(behavior[0])
            weekday_test.append(behavior[1])
            hour_test.append(behavior[2])
            interval_test.append(behavior[3])

        # cut the first bit, of the training data, to make sequences of sequence_length
        if sequence_length != -1:
            # if this sequence is too short to cut anything from, skip it
            # they're all the same length so we only check one
            if sequence_length > len(item_train):
                continue
            item_train = item_train[-sequence_length:]
            weekday_train = weekday_train[-sequence_length:]
            hour_train = hour_train[-sequence_length:]
            interval_train = interval_train[-sequence_length:]

        ITEM_TRAIN[i] = item_train
        ITEM_TEST[i] = item_test
        WEEKDAY_TRAIN[i] = weekday_train
        WEEKDAY_TEST[i] = weekday_test
        HOUR_TRAIN[i] = hour_train
        HOUR_TEST[i] = hour_test
        INTERVAL_TRAIN[i] = interval_train
        INTERVAL_TEST[i] = interval_test
    print("done loading data")


def predict(model):
    relevant = 0.0  # The total number of predictions
    # nDCG where grade for each item is 1
    # nDCG  = DCG/IDCG where IDCG = 1/1
    # because in ideal case, item should be in first position
    nDCG = 0
    nDCG_full = 0
    hit_at_10 = 0

    neg_nDCG = 0
    neg_hit_at_10 = 0

    numUsers = 0  # num of users

    for n in ITEM_TEST.keys():
        item_train = ITEM_TRAIN[n]
        item_test = ITEM_TEST[n]
        hour_train = HOUR_TRAIN[n]
        hour_test = HOUR_TEST[n]
        weekday_train = WEEKDAY_TRAIN[n]
        weekday_test = WEEKDAY_TEST[n]
        interval_train = INTERVAL_TRAIN[n]
        interval_test = INTERVAL_TEST[n]

        h = None
        h2 = None
        h3 = None
        logits = None

        # n represents each user cart, we increment user count
        numUsers += 1

        # Calculate the hidden layer corresponding to the state to be predicted
        for i in range(len(item_train)):
            inputX = item_train[i]
            hourX = hour_train[i]
            weekdayX = weekday_train[i]
            intervalX = interval_train[i]
            inputX = inputX.unsqueeze(0)
            hourX = hourX.unsqueeze(0)
            weekdayX = weekdayX.unsqueeze(0)
            intervalX = intervalX.unsqueeze(0)

            logits, h, h2, h3 = model(inputX, hourX, weekdayX, intervalX, h=h, h2=h2, h3=h3)

        # Forecast, from where the training example stopped onwards
        for j in range(len(item_test)):
            # Current info (Will be used for next prediction)
            inputX = item_test[j]
            hourX = hour_test[j]
            weekdayX = weekday_test[j]
            intervalX = interval_test[j]

            relevant += 1
            # apply softmax, because this is not part of the network
            probOfItems = F.softmax(logits, dim=1)[0]

            # NORMAL VALIDATION
            # topK returns tuple in the form (sorted values,sorted by index)
            rankTuple = torch.topk(probOfItems, TOP)
            rank_index_list = rankTuple[1]

            # use the prediction to see if test is in there
            if item_test[j] in rank_index_list:
                matchPosition = ((rank_index_list == item_test[j]).nonzero())
                index = matchPosition.item()
                # Remember index starts at 0 so +1 to get actual index
                # +1 more because of nDCG formula
                nDCG += 1 / getLog2AtK((index + 1) + 1)
                hit_at_10 += 1

            # START NEGATIVE SAMPLING
            # Get logits for our boy
            test_index = item_test[j]
            test_logits = probOfItems[test_index]
            items = [test_logits]
            # Sample 100 negative boys
            for _ in range(100):
                neg_index = genNegItem(item_test)
                neg = probOfItems[neg_index]
                items.append(neg)

            # Proceed as normal, but with a subset of items
            sampled_items = torch.tensor(items).to(device)

            # topK returns tuple in the form (sorted values,sorted by index)
            neg_rankTuple = torch.topk(sampled_items, TOP)
            neg_rank_index_list = neg_rankTuple[1]  # get the indices

            # use the prediction to see if test is in there
            # TODO: index klopt niet voor deze match..!
            # TODO: alleen laatste item predicten
            # because we start with our positive example at index 0
            match_index = torch.tensor(0).to(device)
            if match_index in neg_rank_index_list:
                matchPosition = (neg_rank_index_list == match_index).nonzero()
                index = matchPosition.item()
                # Remember index starts at 0 so +1 to get actual index
                # +1 more because of nDCG formula
                neg_nDCG += 1 / getLog2AtK((index + 1) + 1)
                neg_hit_at_10 += 1

            # END NEGATIVE SAMPLING

            # calculate the nDCG over all items
            rankFullTuple = torch.topk(probOfItems, ITEM_SIZE)
            indexList = rankFullTuple[1]  # get the indices
            matchPosition = (indexList == item_test[j]).nonzero()
            # Remember index starts at 0 so +1 to get actual index
            # +1 more because of nDCG formula
            nDCG_full += 1 / getLog2AtK((matchPosition + 1) + 1)

            # Recurrent
            inputX = inputX.unsqueeze(0)
            hourX = hourX.unsqueeze(0)
            weekdayX = weekdayX.unsqueeze(0)
            intervalX = intervalX.unsqueeze(0)
            logits, h, h2, h3 = model(inputX, hourX, weekdayX, intervalX, h=h, h2=h2, h3=h3)

    # average over number of queries
    nDCG = nDCG / relevant
    nDCG_full = nDCG_full / relevant
    hit_at_10 = hit_at_10 / relevant

    print('hit@10: ' + str(hit_at_10))
    print('nDCG@10: ' + str(nDCG))
    print('nDCG_full: ' + str(nDCG_full))

    # NEGATIVE SAMPLING
    neg_hit_at_10 = neg_hit_at_10 / relevant
    neg_nDCG = neg_nDCG / relevant

    print('NEGATIVE hit@10: ' + str(neg_hit_at_10))
    print('NEGATIVE nDCG@10: ' + str(neg_nDCG))

    print('ITEM_SIZE: ' + str(ITEM_SIZE))
    print('numUsers: ' + str(numUsers))
    print('relevant(number of test item): ' + str(relevant))
    return hit_at_10, nDCG

def learn(model, optimizer, criterion, data_loader):
    sumloss = 0
    for iteration, batch_inputs in enumerate(data_loader):
        max_length = 0
        for id in batch_inputs:
            id = id.item()
            if len(ITEM_TRAIN[id]) > max_length:
                max_length = len(ITEM_TRAIN[id])

        user_cart = []
        hour_cart = []
        weekday_cart = []
        interval_cart = []

        # pad the inputs to the longest boy
        for i, id in enumerate(batch_inputs):
            id = id.item()
            tmp_user_cart = ITEM_TRAIN[id]
            tmp_hour_cart = HOUR_TRAIN[id]
            tmp_weekday_cart = WEEKDAY_TRAIN[id]
            tmp_interval_cart = INTERVAL_TRAIN[id]

            if len(tmp_user_cart) < max_length:
                difference = max_length - len(tmp_user_cart)
                user_cart.append([0] * difference + tmp_user_cart.tolist())
                hour_cart.append([0] * difference + tmp_hour_cart.tolist())
                weekday_cart.append([0] * difference + tmp_weekday_cart.tolist())
                interval_cart.append([0] * difference + tmp_interval_cart.tolist())
            else:
                user_cart.append(tmp_user_cart.tolist())
                hour_cart.append(tmp_hour_cart.tolist())
                weekday_cart.append(tmp_weekday_cart.tolist())
                interval_cart.append(tmp_interval_cart.tolist())

        # process
        user_cart = torch.tensor(user_cart).to(device)
        hour_cart = torch.tensor(hour_cart).to(device)
        weekday_cart = torch.tensor(weekday_cart).to(device)
        interval_cart = torch.tensor(interval_cart).to(device)

        loss = 0
        optimizer.zero_grad()
        h = None
        h2 = None
        h3 = None

        # We do not input the last item, as this is the target
        for j in range(user_cart.shape[1] - 1):
            inputX = user_cart[:, j]
            hourX = hour_cart[:, j]
            weekdayX = weekday_cart[:, j]
            intervalX = interval_cart[:, j]
            label = user_cart[:, j + 1]
            logits, h, h2, h3 = model(inputX, hourX, weekdayX, intervalX, h=h, h2=h2, h3=h3)
            loss += criterion(logits, label)
        loss.backward()
        optimizer.step()
        sumloss += loss
    print('sumloss: ' + str(float(sumloss)))
    return model

def saveCheckpoint(state):
    torch.save(state, MODEL_FILE)

def loadCheckpoint(filename):
    device = "cuda" if usingCuda() else "cpu"
    return torch.load(filename, map_location=device)


def initLogOfIndexes():
    global LOG_OF_INDEXES
    isCuda = usingCuda()
    sizeOfArr = ITEM_SIZE + 2
    if isCuda:
        cuda_str = 'cuda:' + str(torch.cuda.current_device())
        LOG_OF_INDEXES = torch.log2(torch.arange(1., sizeOfArr, device=cuda_str))
    else:
        LOG_OF_INDEXES = torch.log2(torch.arange(1., sizeOfArr, dtype=torch.float64))


def genNegItem(user_cart):
    item = np.random.randint(1, ITEM_SIZE)
    while item in user_cart:
        item = np.random.randint(1, ITEM_SIZE)
    return item


def getLog2AtK(k):
    global LOG_OF_INDEXES
    position = k - 1
    return LOG_OF_INDEXES[position].item()


def genTensor(tensorObj, tensorType=None):
    isCuda = usingCuda()
    if tensorType == None and isCuda:
        return torch.cuda.FloatTensor(tensorObj)
    elif tensorType == None and not isCuda:
        return torch.FloatTensor(tensorObj)
    elif tensorType == LONG_STR and isCuda:
        return torch.cuda.LongTensor(tensorObj)
    elif tensorType == LONG_STR and not isCuda:
        return torch.LongTensor(tensorObj)


def usingCuda():
    return (torch.cuda.is_available() and RUN_CUDA)


def main(args):
    global ITEM_TEST, ITEM_TRAIN
    print('ITEM_SIZE: ' + str(ITEM_SIZE))
    print('USER_SIZE: ' + str(USER_SIZE))

    pre_data(sequence_length=args.sequence_length, split=args.split)
    print('ITEM_TRAIN.keys(): ' + str(len(ITEM_TRAIN.keys())))
    for i in ITEM_TRAIN.keys():
        for k in range(len(INTERVAL_TRAIN[i])):
            INTERVAL_TRAIN[i][k] += 1
        for k in range(len(INTERVAL_TEST[i])):
            INTERVAL_TEST[i][k] += 1

        ITEM_TRAIN[i] = genTensor(ITEM_TRAIN[i], tensorType=LONG_STR)
        HOUR_TRAIN[i] = genTensor(HOUR_TRAIN[i], tensorType=LONG_STR)
        WEEKDAY_TRAIN[i] = genTensor(WEEKDAY_TRAIN[i], tensorType=LONG_STR)
        INTERVAL_TRAIN[i] = genTensor(INTERVAL_TRAIN[i])

        ITEM_TEST[i] = genTensor(ITEM_TEST[i], tensorType=LONG_STR)
        HOUR_TEST[i] = genTensor(HOUR_TEST[i], tensorType=LONG_STR)
        WEEKDAY_TEST[i] = genTensor(WEEKDAY_TEST[i], tensorType=LONG_STR)
        INTERVAL_TEST[i] = genTensor(INTERVAL_TEST[i])


    model = srnn.SRNNModel(hidden_size=HIDDEN_SIZE,
                           weekday_size=WEEKDAY_SIZE,
                           hour_size=HOUR_SIZE,
                           num_class=ITEM_SIZE,
                           isCuda=usingCuda(),
                           mode=MODE)

    criterion = torch.nn.CrossEntropyLoss()
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)

    if (usingCuda()):
        model.cuda()

    load_model_from = MODEL_FILE if args.model_path == "" else args.model_path
    print("Attempting to load model from:", load_model_from)
    if os.path.exists(load_model_from):
        print("Stored model found. Continuing from model", load_model_from)
        checkpoint = loadCheckpoint(load_model_from)
        model.load_state_dict(checkpoint["model"])
        optimizer.load_state_dict(checkpoint["optim"])
        model.train()
    else:
        print("No model was found at this path. Starting from scratch.")

    if args.evaluate_only:
        model.eval()
        predict(model)
        return

    # start training & testing
    epoch = 0
    print("starting learning")
    print("MODEL", model)
    data_loader = get_dataloader(ITEM_TRAIN, HOUR_TRAIN, WEEKDAY_TRAIN, INTERVAL_TRAIN)

    while (epoch <= 100):
        print("Epoch %d" % epoch)
        print("Training...")

        learn(model, optimizer, criterion, data_loader)
        print("begin predict, number of test items", len(ITEM_TEST))
        hit_at_10, nDCG = predict(model)
        if (nDCG > BEST_NDCG):
            saveCheckpoint({
                'hiddenSize': HIDDEN_SIZE,
                'DATANAME': DATANAME,
                'BEST_NDCG': BEST_NDCG,
                'epoch': epoch,
                'optim': optimizer.state_dict(),
                'model': model.state_dict()
            })
            epoch += 1

    print('FINISHED LEARNING!')

def createFolder(folderName):
    if not os.path.exists(folderName):
        os.makedirs(folderName)


# python main.py (cuda) (file) (model)
# $ python main.py 1 miniData STAR 

if __name__ == '__main__':
    torch.manual_seed(42)
    torch.set_printoptions(threshold=5000)

    HIDDEN_SIZE = 40
    device = 'cpu'
    parser = argparse.ArgumentParser()

    # DATASET PARAMETERS
    parser.add_argument('--cuda', default=False, help='Use cuda')
    parser.add_argument('--dataset', required=True, help='Location of pre-processed dataset')
    parser.add_argument('--model', default="STAR", help='Model used. Choose from {STAR, SITAR}')
    parser.add_argument('--model_path', default="", help='Path of a stored model to use.')
    parser.add_argument('--seed', default=42, type=int, help="random seed used to generate batches and negative samples")
    parser.add_argument('--split', default=0.8, type=float, help="Training / test split ratio. Use 1 to only predict the very last item in the sequence.")
    parser.add_argument('--evaluate_only', type=bool, default=False, help="Set to True if you have a trained model and only want to evaluate")
    parser.add_argument('--sequence_length', type=int, default=-1, help="If set, uses the last x items from the sequence to make prediction")
    args = parser.parse_args()

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    if args.sequence_length > 200:
        print("we use maximum length of 200 in our experiments")
        sys.exit(0)

    if "movielens" in args.dataset.lower() or "ml-1m" in args.dataset.lower():
        DATANAME = 'movielens'
    elif "books" in args.dataset.lower():
        DATANAME = 'Books'
    elif "beauty" in args.dataset.lower():
        DATANAME = 'Beauty'
    else:
        DATANAME = args.dataset.split("/")[-1].split(".")[0]

    DATAFILE = args.dataset
    if args.model != srnn.STAR and args.model != srnn.SITAR:
        print("incorrect model name given")
        sys.exit(0)
    MODE = args.model

    OUTPUT_PATH = './output/results/'
    MODEL_DIR = './output/model/'
    USER_SIZE = 1
    itemFreq = {}
    data = open(DATAFILE, 'r')
    lines = data.readlines()
    for line in lines:
        cart = json.loads(line)
        USER_SIZE += 1
        for element in range(len(cart)):
            curItem = cart[element][0]
            if curItem not in itemFreq:
                itemFreq[curItem] = 1

    ITEM_SIZE = len(itemFreq) + 1

    # We add one because we do not want to start id from 0
    HOUR_SIZE = 24 + 1  # (hours)
    WEEKDAY_SIZE = 7 + 1  # (day)
    # We do not want 0 interval so we add 1
    INTERVAL_SIZE = 32 + 1

    createFolder(OUTPUT_PATH)
    createFolder(MODEL_DIR)

    # Clear file
    OUTPUT_FILE = OUTPUT_PATH + MODE+'_' + DATANAME + '_' + str(HIDDEN_SIZE) + '.txt'
    MODEL_FILE = MODEL_DIR + MODE+'_' + DATANAME + '_' + str(HIDDEN_SIZE) + '.mdl'
    with open(OUTPUT_FILE, "w") as the_file:
        the_file.write("")

    print('OUTPUT_FILE: ' + str(OUTPUT_FILE))
    print('RUN_CUDA: ' + str(RUN_CUDA))
    print('DATAFILE: ' + str(DATAFILE))
    print('usingCuda(): ' + str(usingCuda()))
    print('HIDDEN_SIZE: ' + str(HIDDEN_SIZE))

    initLogOfIndexes()
    start = time.time()
    main(args)
    end = time.time()
    hours, rem = divmod(end - start, 3600)
    minutes, seconds = divmod(rem, 60)
    print("{:0>2}:{:0>2}:{:05.2f}".format(int(hours), int(minutes), seconds))
