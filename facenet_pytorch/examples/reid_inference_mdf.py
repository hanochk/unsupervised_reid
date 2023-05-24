from logging import warning
import sys
import os
import copy
import time
import shutil
sys.path.append('/home/hanoch/notebooks/nebula3_reid')
sys.path.append('/home/hanoch/notebooks/nebula3_reid/facenet_pytorch')
sys.path.append('/home/hanoch/notebooks/nebula3_reid/facenet_pytorch/examples')
curr_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.append(curr_dir)
directory = os.path.abspath(__file__)
# # setting path
# sys.path.append(directory.parent.parent)

REID_DIR = os.path.abspath(os.path.join(os.path.join(os.path.join(__file__, os.pardir), os.pardir), os.pardir))
os.path.abspath(os.path.join(__file__, os.pardir))

import numpy as np
import matplotlib.pyplot as plt
import tqdm
import pickle
from PIL import Image, ImageDraw
import PIL.ImageFont as ImageFont
import PIL.ImageColor as ImageColor
import matplotlib.colors as mcolors

import cv2
import re
from collections import Counter
import pandas as pd
import glob
import subprocess
# test
##### from nebula3_reid.facenet_pytorch.examples.clustering import dbscan_cluster, _chinese_whispers
from examples.clustering import hdbscan_dbscan_cluster, _chinese_whispers, hdbscan_cluster
from facenet_pytorch import MTCNN, InceptionResnetV1, extract_face, fixed_image_standardization
# from facenet_pytorch.models import mtcnn, inception_resnet_v1
import torch
from torch.utils.data import DataLoader
from torchvision import datasets
from argparse import ArgumentParser
from examples.face_reid_util import p_r_plot_multi_class, umap_plot
import torchvision.transforms as T
transform = T.ToPILImage()
import warnings
sys.path.append(REID_DIR)
from weaviate_db.db_connection import WeaviateDB

operational_mode = False #False # True:default param
# @@HK for Ghandi : min_face_res=128 where dense MDF incease filtering otherwise minor characters clutter the face classification
# min_ids_per_cluster_sparse_mdf = 2

class EmbeddingsCollect():
    def __init__(self):
        self.embed = list()
        self.label = list()
        self.bbox_area = list()
        return

try:
    # font = ImageFont.truetype('arial.ttf', 24)
    font = ImageFont.truetype("Tests/fonts/FreeMono.ttf", 26)
except IOError:
    font_path = os.path.join(cv2.__path__[0], 'qt', 'fonts', 'DejaVuSans.ttf')
    font = ImageFont.truetype(font_path, size=26)

    # font = ImageFont.load_default()

    # font = ImageFont.truetype("Tests/fonts/FreeMono.ttf", 84) #ImageFont.load_default()  font = ImageFont.load_default()

color_space = [ImageColor.getrgb(n) for n, c in ImageColor.colormap.items()][7:] # avoid th aliceblue a light white one
not_id = -1
# [n for n, c in ImageColor.colormap.items()]

def print_arguments(args):
    """Print the specified map object ordered by key; one line per mapping"""
    header = "Command line arguments:"
    print('\n' + header)
    print(len(header) * '-')
    args_dict = vars(args)
    arguments_str = '\n'.join(["{}: {}".format(key, args_dict[key]) for key in sorted(args_dict)])
    print(arguments_str + '\n')


class FaceReId:

    # init method or constructor
    def __init__(self, margin=40, min_face_res=64, re_id_method={'method': 'dbscan', 'cluster_threshold': 0.27, 'min_cluster_size': 5},
                 simillarity_metric='cosine',
                 prob_th_filter_blurr=0.95, batch_size=128, id_to_mdf_ratio=10, plot_fn=False, recluster_hard_positives=False,
                 use_actor_name_db=True, actor_name_retrival_cos_dist_th=0.32):# actor_name_retrival_cos_dist_th was 0.3 due to moving to 720p
        self.margin = margin
        self.min_res_for_embed_for_actor_retrival = 96 # basis for avg_pooling based on high res images out of the ID cluster
        self._min_face_res = min_face_res
        self.prob_th_filter_blurr = prob_th_filter_blurr
        self.re_id_method = re_id_method
        self.batch_size = batch_size
        self.simillarity_metric = simillarity_metric
        self.plot_fn = plot_fn
        self.recluster_hard_positives = recluster_hard_positives
        # hard positives which are farther away from their cluster
        self.min_prob_hard_pos_reassign = 0.98
        self.min_res_hard_pos_reassign = 96**2 #256**2
        self.covariance_type = 'diag' #'full' #'diag'
        self.id_to_mdf_ratio = id_to_mdf_ratio # Sparse MDFs in hollywood2 hence empirically determine the threshold to swithch to sparse mode

        self.reassign_classification_ratio = -1
        self.basic_classification_ratio = -1

        if self.min_face_res >= 96:
            self.clustering_cos_dist_sparse_mdf = 0.4
        else:
            self.clustering_cos_dist_sparse_mdf = 0.4#0.35 # for lower resolution embeddings are less separable hence strict mimimnal cos_dist


        self.min_ids_per_cluster_sparse_mdf = 2 # for short clips aka sparse MDFs
        self.delta_thr_sparse_mdfs = self.clustering_cos_dist_sparse_mdf - self.re_id_method['cluster_threshold']# 0.2# 0.15#0.1 # heuristic  S5 hollywood2

        self.device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
        print('Running on device: {}'.format(self.device))
        self.use_actor_name_db = use_actor_name_db
        if use_actor_name_db:
            self.id_to_actor_name = dict()
            self.weaviate_instance = WeaviateDB()
            self.actor_name_retrival_cos_dist_th = actor_name_retrival_cos_dist_th
        # Modify model to VGGFace based and resnet
        self.model_latest = False

        if self.model_latest:
            sys.path.append(
                '/home/hanoch/notebooks/nebula3_reid/Automated_Face_Tracking_and_Labelling')  # ID_discriminator.py
            from Automated_Face_Tracking_and_Labelling.models.ID_discriminator import senet50_256
            self.model = senet50_256(weights_path='senet50_256_pytorch.pth').eval().to(self.device)
        # MTCNN image_size should be 224*224
        #     Consider preprocessing with the method but it does normalization inside assuming non normalized face patch which is in contrast to MTCNN output /
        #     : https://github.com/Andrew-Brown1/Automated_Face_Tracking_and_Labelling/blob/9c094170117837ec0c919ae4b65b83105b1f1908/models/model_datasets.py#L156
        else:
            self.model = InceptionResnetV1(pretrained='vggface2').eval().to(self.device)  # TODO
        """
        Try zisserman/Andrew-Brown : new model VGGFACE2 model based on SE-ResNet  https://github.com/ox-vgg/vgg_face2#pre-processing https://github.com/Andrew-Brown1/Automated_Face_Tracking_and_Labelling
        pip install -U --no-cache-dir gdown --pre
        url = 'https://drive.google.com/u/0/uc?id=1zQep6lsG2SS39sj0fJmkji07A_tl2v44&export=download'
        output = 'model.pth'
        gdown.download(url, output)
        import imp
        import torch
        MainModel = imp.load_source('MainModel', 'senet50_256_pytorch.py')
        model = torch.load('senet50_256_pytorch.pth')
        random_feat = model(torch.randn(1, 3, 224, 224))
        print(random_feat.squeeze().size())
        sys.path.append('/home/hanoch/notebooks/nebula3_reid/Automated_Face_Tracking_and_Labelling') #ID_discriminator.py
        from Automated_Face_Tracking_and_Labelling.models.ID_discriminator import senet50_256
        model = senet50_256(weights_path='senet50_256_pytorch.pth')        
        """

    # a getter function
    @property
    def min_face_res(self):
        # print("getter method called for min_face_res")
        return self._min_face_res

    # a setter function
    @min_face_res.setter
    def min_face_res(self, a):
        if (a < 8):
            raise ValueError("min_face_res too low")
        # print("setter min_face_res method called")
        self._min_face_res = a
        # Re asess parameters
        if self._min_face_res >= 96:
            self.clustering_cos_dist_sparse_mdf = 0.4
        else:
            self.clustering_cos_dist_sparse_mdf = 0.4#0.35 # for lower resolution embeddings are less separable hence strict mimimnal cos_dist  # less than 0.4 take out Id from 1679481985_actioncliptest00013/re-id_frame0618.jpg
        self.delta_thr_sparse_mdfs = self.clustering_cos_dist_sparse_mdf - self.re_id_method['cluster_threshold']# 0.2# 0.15#0.1 # heuristic  S5 hollywood2

# Sample Method
    def extract_faces(self, path_mdf, result_path_good_resolution_faces, **kwargs):
          # latest model from vggface2 SeNet50_256 not having good clustering result
        if self.model_latest:
            image_size = 224
        else:
            image_size = 160  # Basic for the FaceNEt to be trained over

        keep_all = True  # HK revert
        # post_process=True => fixed_image_standardization : (image_tensor - 127.5) / 128.0 = > and hence the cropped face is restandarized to be ready for the next NN
        #   The Proposal Network (P-Net) The Refine Network (R-Net) The Output Network (O-Net) +batched_nms @th=0.5
        self.mtcnn = MTCNN(
            image_size=image_size, margin=self.margin, min_face_size=self.min_face_res,
            thresholds=[0.6, 0.7, 0.7], factor=0.709, post_process=True, keep_all=keep_all,
            device=self.device)  # post_process=False

        # rel_path = 'nebula3_reid/facenet_pytorch'
        plot_cropped_faces = kwargs.pop('plot_cropped_faces', False)
        # TODO result_path_good_resolution_faces_frontal =  # filter profile
        # plt.savefig('/home/hanoch/notebooks/nebula3_reid/face_tens.png')
        # workers = 0 if os.name == 'nt' else 4

        detection_with_landmark = False
        if plot_cropped_faces:
            print("FaceNet output is post process : fixed_image_standardization")

        #### Define MTCNN module
        """
        Default params shown for illustration, but not needed. Note that, since MTCNN is a collection of neural nets and other code, the device must be passed in the following way to enable copying of objects when needed internally.
        See `help(MTCNN)` for more details.
        """
        aligned = list()
        names = list()
        mtcnn_cropped_image = list()

        # mdf_filenames = [os.path.join(path_mdf, x) for x in os.listdir(path_mdf)
        #
          #                     if x.endswith('png') or x.endswith('jpg')]
        if 1: # for MPii dataset images are nested inside actor name subfolders
            mdf_filenames = glob.glob(path_mdf + '/**/*.jpg', recursive=True) + glob.glob(path_mdf + '/**/*.png', recursive=True) + \
                                                glob.glob(path_mdf + '/**/*.jpeg', recursive=True)
        else:
            mdf_filenames = [os.path.join(path_mdf, x) for x in os.listdir(path_mdf)
                       if x.endswith('jpg') or x.endswith('jpeg') or x.endswith('png')]

        if not bool(mdf_filenames):
            raise ValueError('No files at that folder')

        status = True
        mdf_id_all = dict()
        for file_inx, file in enumerate(tqdm.tqdm(sorted(mdf_filenames))):
            try:
                img = Image.open(file)  # print(Image.core.jpeglib_version) ver 9.0 on conda different jpeg decoding  '3001_21_JUMP_STREET_00.03.13.271-00.03.16.551'
            except Exception as e:
                print(e)
                status = False
                continue
            if 0:  # direct
                x_aligned, prob = mtcnn(img, return_prob=True)
            else:
                try:  # Face landmarks + embeddings of aligned face
                    batch_boxes, prob, lanmarks_points = self.mtcnn.detect(img, landmarks=True)
                    x_aligned = self.mtcnn.extract(img, batch_boxes, save_path=None)  # implicitly saves the faces
                except Exception as ex:
                    print(ex)
                    status = False
                    continue

            if x_aligned is not None:
                face_id = dict()
                if len(x_aligned.shape) == 3:
                    x_aligned = x_aligned.unsqueeze(0)
                    prob = np.array([prob])
                for crop_inx in range(x_aligned.shape[0]):
                    if prob[crop_inx] > self.prob_th_filter_blurr: # TODO since MTCNN doesn;t filter according to min_size add condition any([(x[2] - x[0] >self.min_face_res and x[3] - x[1]>self.min_face_res) for x in batch_boxes])
                        face_tens = x_aligned[crop_inx, :, :, :].squeeze().permute(1, 2, 0).cpu().numpy()
                        face_bb_resolution = 'res_ok'
                        # for p in lanmarks_points:
                        #     draw.rectangle((p - 1).tolist() + (p + 1).tolist(), width=2)

                        # img2 = cv2.cvtColor(face_tens, cv2.COLOR_RGB2BGR)  # ???? RGB2BGR
                        img2 = cv2.cvtColor(face_tens, cv2.COLOR_BGR2RGB)  # undo
                        # img2 = Image.fromarray((face_tens * 255).astype(np.uint8))
                        normalizedImg = np.zeros_like(img2)
                        normalizedImg = cv2.normalize(img2, normalizedImg, 0, 255, cv2.NORM_MINMAX)
                        img2 = normalizedImg.astype('uint8')
                        window_name = os.path.basename(file)
                        # cv2.imshow(window_name, img)

                        # cv2.setWindowTitle(window_name, str(movie_id) + '_mdf_' + str(mdf) + '_' + caption)
                        # cv2.putText(image, caption + '_ prob_' + str(lprob.sum().__format__('.3f')) + str(lprob),
                        #             fontFace=cv2.FONT_HERSHEY_SIMPLEX, fontScale=0.5, color=(0, 0, 255), thickness=2,
                        #             lineType=cv2.LINE_AA, org=(10, 40))
                        save_path = os.path.join(result_path_good_resolution_faces,
                                                 str(file_inx) + '_prob_' + str(
                                                     prob[crop_inx].__format__('.2f')) + '_' + str(
                                                     face_tens.shape[0]) + '_face_{}'.format(
                                                     crop_inx) + os.path.basename(file))
                        if plot_cropped_faces:  # The normalization handles the fixed_image_standardization() built in in MTCNN forward engine
                            cv2.imwrite(save_path,
                                        img2)  # (image * 255).astype(np.uint8))#(inp * 255).astype(np.uint8))

                        if 0:  # save MDFs
                            cv2.imwrite(os.path.join(result_path, str(crop_inx) + '_' + os.path.basename(file)),
                                        img2)  # (image * 255).astype(np.uint8))#(inp * 255).astype(np.uint8))
                        is_exist_key_in_dict = mdf_id_all.get(os.path.basename(file), None) # in MPii dataset same image can appear in few IDs folders hence replicated names list, so once all faces has been detected no need to re-detect
                        if is_exist_key_in_dict is None: # No worries for the mdf_id_all dictionary it will be overwridden by the same image re-processed
                            mtcnn_cropped_image.append(img2)
                            aligned.append(x_aligned[crop_inx, :, :, :])
                            fname = str(file_inx) + '_' + '_face_{}'.format(crop_inx) + '_' + os.path.basename(file)
                            names.append(fname)
                            face_id.update({fname: {'bbox': batch_boxes[crop_inx], 'id': -1, 'gt': -1, 'prob': prob[crop_inx]}})
                        else:
                            print("Face det replicated entry skipped!!")
                        # print('Face detected with probability: {:8f}'.format(prob[crop_inx]))
                if bool(face_id):  # Cases where none of the prob>th
                    mdf_id_all.update({os.path.basename(file): face_id})

        a = 0
        for k1, v1 in mdf_id_all.items():
            a += len(list(v1))
        assert (a == len(names)) # should be complied

        if aligned == []:
            print("No faces have been found in any of MDFs !!!! ")
            status = False
            return None, None, None, None, status

        all_embeddings = facenet_embeddings(aligned, self.batch_size,
                                            image_size=self.mtcnn.image_size, device=self.device, neural_net=self.model)



        return all_embeddings, mtcnn_cropped_image, names, mdf_id_all, status
        # embeddings = resnet(aligned).detach().cpu()
        ##TODO: try cosine similarity
        ## TODO : vs GT add threshold -> calc Precision recall infer threshold->run over testset

    def assign_actor_name_to_id_cluster(self, labeled_embed, mdf_id_all, names,
                                        all_embeddings, input_type='image'):

        id_to_mean_embedd = dict()
        high_res_id_uniq = np.empty([1])
        if input_type != "image":
            high_res_id = np.unique(np.array(labeled_embed.label)[np.where(np.array(labeled_embed.bbox_area) > self.min_res_for_embed_for_actor_retrival**2)[0]])
            if len(high_res_id)>0 and input_type != "image":
                ind_of_high_res_id = np.where(np.array(labeled_embed.bbox_area) > self.min_res_for_embed_for_actor_retrival ** 2)[0]
                high_res_ids = np.array(labeled_embed.label)[ind_of_high_res_id]
                high_res_id_uniq = np.unique(high_res_ids)
                for high_res_id_uniq_ in high_res_id_uniq:
                    mean_embedding = torch.mean(torch.stack([labeled_embed.embed[ix] for ix in ind_of_high_res_id[high_res_ids == high_res_id_uniq_]]), dim=0)
                    id_to_mean_embedd[high_res_id_uniq_] = mean_embedding

        # id_to_mean_embedd = dict()
        if input_type != "image":
            for idx, cur_id in enumerate(labeled_embed.label):
                if cur_id not in high_res_id_uniq: # was already processed based on high res images
                    if cur_id not in id_to_mean_embedd:
                        id_to_mean_embedd[cur_id] = [labeled_embed.embed[idx]]
                    else:
                        id_to_mean_embedd[cur_id].append(labeled_embed.embed[idx])

            for cur_id, embeddings in id_to_mean_embedd.items():
                if isinstance(id_to_mean_embedd[cur_id], list): # hence it is a list of tensor before averaging while other are high res pre_computed
                    mean_embedding = torch.mean(torch.stack(embeddings), dim=0)
                    id_to_mean_embedd[cur_id] = mean_embedding
        else:
            for idx, _ in enumerate(names):
                if idx not in id_to_mean_embedd:
                    id_to_mean_embedd[idx] = torch.unsqueeze(all_embeddings[idx], dim=0)
                # Assuming same person appears only once.

        for face_id, face_mean_embedding in id_to_mean_embedd.items():
            search_output = self.weaviate_instance.search(face_mean_embedding)
            similarity_output = search_output[0]['_additional']['distance']
            print("Similarity distance: {}".format(similarity_output))
            if similarity_output < self.actor_name_retrival_cos_dist_th:
                chosen_actor_name = search_output[0]['actor_name']
                self.id_to_actor_name[face_id] = {"actor_name": chosen_actor_name}
            else:
                self.id_to_actor_name[face_id] = {"actor_name": ''}

            if input_type == "image":
                image_name = list(mdf_id_all.keys())[0]
                id_image_name = list(mdf_id_all[list(mdf_id_all.keys())[0]].keys())[face_id]
                if id_image_name in mdf_id_all[image_name]:
                    mdf_id_all[image_name][id_image_name]['id'] = face_id

        return
    def re_identification(self, all_embeddings, mtcnn_cropped_image, names,
                            mdf_id_all, result_path, metric='cosine'):

        dbscan_result_path = os.path.join(result_path, self.re_id_method['method'])
        if dbscan_result_path and not os.path.exists(dbscan_result_path):
            os.makedirs(dbscan_result_path)

        if self.re_id_method['method'] == 'similarity':
            top_k = 3
            dists = [[(e1 - e2).norm().item() for e2 in all_embeddings] for e1 in all_embeddings] # better cosine : [np.dot(hard_pos_id_0,  e2)/(np.linalg.norm(e2)*np.linalg.norm(hard_pos_id_0)).item() for e2 in all_embeddings]
            # all_similar_face_mdf = list()
            dist_per_face = torch.from_numpy(np.array(dists).astype('float32'))
            v_top_k, i_topk = torch.topk(-dist_per_face, k=top_k, dim=1) # topk of -dist is mink of dist idenx 0 is 1 vs. the same 1.
            for i in range(all_embeddings.shape[0]):
                for t in range(top_k):
                    print("pairwise match to face {} : {} is {} \n ".format(i, names[i], names[i_topk[i][t]]))
            # for mdfs_ix in range(all_embeddings.shape[0]):
            #     similar_face_mdf = np.argmin(np.array(dists[mdfs_ix])[np.where(np.array(dists[mdfs_ix])!=0)]) # !=0 is the (i,i) items which is one vs the same
            #     all_similar_face_mdf.append(similar_face_mdf)
        elif self.re_id_method['method'] == 'dbscan' or self.re_id_method['method'] == 'hdbscan':  # @@HK TODO: use the method db.fit_predict() to predict additional embeddings class given the dnscan object inside dbscan_cluster
            clusters = hdbscan_dbscan_cluster(images=mtcnn_cropped_image, matrix=all_embeddings,
                            out_dir=dbscan_result_path, cluster_threshold=self.re_id_method['cluster_threshold'],
                            min_cluster_size=self.re_id_method['min_cluster_size'], metric=metric, method=self.re_id_method['method'])
            # when cosine dist ->higher =>more out of cluster(non core points) are gathered and became core points as in clusters hence need to increase the K-NN, cluster size
            n_clusters = len([i[0] for i in clusters.items()])

            if n_clusters < 2:
                warning("too few classes/IDs < 2 !!!")
            if clusters:
                print("Total {} clusters and total appeared IDs {}".format(n_clusters,
                                            np.concatenate([x[1] for x in clusters.items()]).shape[0]))
            else: # crash program in case too few MDFs and no IDs found then 1 cluster per ID min_cluster_size=1 is not valid cuz no reID in single ID appearance
                print("Re run Re-ID : Could not find recurrent ID assume single ID per MDF - minimal min_cluster_size(K-NN)= ceil(min_cluster_size/2) performance not guaranteed!!!")
                if self.re_id_method['method'] == 'dbscan':
                    self.re_id_method['min_cluster_size'] = 1#int(1 + self.re_id_method['min_cluster_size']/2)
                    self.re_id_method['cluster_threshold'] = self.re_id_method[
                                                                 'cluster_threshold'] + self.delta_thr_sparse_mdfs/2
                    self.re_id_method['cluster_threshold'] = float(
                        "{:.2f}".format(self.re_id_method['cluster_threshold']))

                elif self.re_id_method['method'] == 'hdbscan':
                    pass
                else:
                    raise
                clusters = hdbscan_dbscan_cluster(images=mtcnn_cropped_image, matrix=all_embeddings,
                                out_dir=dbscan_result_path, cluster_threshold=self.re_id_method['cluster_threshold'],
                                min_cluster_size=self.re_id_method['min_cluster_size'], metric=metric,
                                          method=self.re_id_method['method'])
                # when cosine dist ->higher =>more out of cluster(non core points) are gathered and became core points as in clusters hence need to increase the K-NN, cluster size
                n_clusters = len([i[0] for i in clusters.items()])
                if clusters:
                    print("Total {} clusters and total appeared IDs {}".format(n_clusters,
                                                np.concatenate([x[1] for x in clusters.items()]).shape[0]))
                    # In case too few MDFs then the unclusterd with high likelihood assign successive id  #TODO @@HK : put here a func() go over all MDFs in case sparse MDFs and number any character with ascending numbering

        else:
            raise

        single_id_appearance_class_no = n_clusters
        labeled_embed = EmbeddingsCollect()
        # Assign clustering ID to movie dictionary
        for mdf, id_v in mdf_id_all.items():
            mdf_id_list_phantom_id = list()
            mdf_id_dict_phantom_max_prob = dict()
            mdf_id_dict_id_per_face_key_max_prob = dict()
            for k, v in id_v.items():
                if k in names:
                    ix = names.index(k)
                    id_cluster_no = find_key_given_value(clusters, ix)
                    # if (mdf == 'frame0874.jpg' and id_cluster_no == -1): # debug and catch the un classified embedding s
                    #     print("all_embeddings vector is ", ix)
                    if (id_cluster_no != -1):
                        if 1: # phantom handling
                            if id_cluster_no in mdf_id_list_phantom_id: # phantom is detected since that ID already appeared in the MDF
                                max_prob_id = mdf_id_dict_phantom_max_prob.get(id_cluster_no, 0) # max prob so far for specific ID
                                if max_prob_id < mdf_id_all[mdf][k]['prob']: # candidate has greater prob hence it is real and previous is phantom
                                    print("*********  Remove Phantom ID!!! **********", mdf, k, id_cluster_no, max_prob_id, mdf_id_all[mdf][k]['prob'])
                                    key_phantom = mdf_id_dict_id_per_face_key_max_prob.get(id_cluster_no, -100)
                                    if key_phantom == -100: # first time ever
                                        print("BUG mdf_id_dict_id_per_face_key_max_prob was already set")
                                    mdf_id_all[mdf][key_phantom]['id'] = -1 # overwrite with Phantom
                                    mdf_id_all[mdf][k]['id'] = id_cluster_no # update dictionary with ID from clustering assigned ID
                                    mdf_id_dict_id_per_face_key_max_prob.update({id_cluster_no: k})
                            else:
                                mdf_id_all[mdf][k]['id'] = id_cluster_no
                                mdf_id_list_phantom_id.append(id_cluster_no)
                                mdf_id_dict_id_per_face_key_max_prob.update({id_cluster_no: k})

                            # update prob with the max per ID
                            max_prob_id = mdf_id_dict_phantom_max_prob.get(id_cluster_no, 0)
                            if max_prob_id < mdf_id_all[mdf][k]['prob']:
                                mdf_id_dict_phantom_max_prob.update({id_cluster_no: mdf_id_all[mdf][k]['prob']})
                        else:
                            mdf_id_all[mdf][k]['id'] = id_cluster_no # old version

                        labeled_embed.embed.append(all_embeddings[ix])
                        labeled_embed.label.append(id_cluster_no)
                        bbox = v['bbox']
                        id_box_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                        labeled_embed.bbox_area.append(id_box_area)

                    else:
                        # Giving IDs to non clustered/recognized Ids is a bad idea since it gives another IN no to hard positives
                        if self.re_id_method['min_cluster_size'] == self.min_ids_per_cluster_sparse_mdf and 0:  # sparse MDFs
                            bbox = v['bbox']
                            box_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                            if box_area > self.min_res_for_embed_for_actor_retrival ** 2:
                                mdf_id_all[mdf][k]['id'] = single_id_appearance_class_no
                                labeled_embed.embed.append(all_embeddings[ix])
                                labeled_embed.label.append(single_id_appearance_class_no)
                                labeled_embed.bbox_area.append(box_area)
                                single_id_appearance_class_no += 1

        if self.re_id_method['min_cluster_size'] == self.min_ids_per_cluster_sparse_mdf and 0:  # sparse MDFs
            print("Added {} single Image ID  ".format(single_id_appearance_class_no-n_clusters-1))

        print("Only {} [%] of detected faces were classified !!!".format(100*len(labeled_embed.label)/all_embeddings.shape[0]))
        self.basic_classification_ratio = 100*len(labeled_embed.label)/all_embeddings.shape[0]
        # post DBSCAN KNN clustering not assigned while prob is high and bbox is sailence
        if self.recluster_hard_positives and self.basic_classification_ratio < 100: # No point in improving 100%
            self.reassign_em_gmm(mdf_id_all, clustered_labeled_embed=labeled_embed,
                              all_embeddings=all_embeddings, id_key_in_mdf=names)
            # self.reassign_knn(mdf_id_all, clustered_labeled_embed=labeled_embed,
            #                   all_embeddings=all_embeddings, id_key_in_mdf=names,
            #                   knn=self.re_id_method['min_cluster_size'])

            print("Recluster_hard_positives {} [%] of detected faces were classified !!!".format(100*len(labeled_embed.label)/all_embeddings.shape[0]))
            self.reassign_classification_ratio = 100 * len(labeled_embed.label) / all_embeddings.shape[0]
        # cos_sim_hard_pos = [np.dot(hard_pos_id_0,  e2)/(np.linalg.norm(e2)*np.linalg.norm(hard_pos_id_0)).item() for e2 in all_embeddings]
        # distance_hard_pos_to_its_positives = [1 - cos_sim_hard_pos[x] for x in clusters[0]]

        # validation code for no phantom ID anymore
        for k, v in mdf_id_all.items():
            it = list(v.values())
            mdf_dict_list = list(it)
            lbls, cs = np.unique([x['id'] for x in mdf_dict_list], return_counts=True)
            # print(lbls[cs>1][lbls[cs>1]!=-1])
            multi_same_lbl = lbls[cs > 1][lbls[cs > 1] != -1]
            if multi_same_lbl.size > 0:
                print(multi_same_lbl, k)
                print('hueston repetative label in MDF PHANTOM should be filtered already!!!', multi_same_lbl)
                for repet in multi_same_lbl:
                    print(repet)

        return mdf_id_all, labeled_embed
    # lbl, c = np.unique([x['id'] for x in list(list(mdf_id_all.values())[0].values())], return_counts=True)   [x for x in list(mdf_id_all.values())]
    def reassign_em_gmm(self, mdf_id_all, clustered_labeled_embed, all_embeddings,
                        id_key_in_mdf, collect_statistics_training=False):
        from sklearn.mixture import GaussianMixture
        if collect_statistics_training:
            all_no_det_clip_key = list()
            csv_file = os.path.join('/media/media/services', 'gmm_hist.csv')

        try:
            all_mean_culsters = list()
            high_res_ids = np.array(clustered_labeled_embed.label)
            high_res_id_uniq = np.unique(high_res_ids)
            for high_res_id_uniq_ in high_res_id_uniq:
                mean_embedding = torch.mean(
                    torch.stack([clustered_labeled_embed.embed[ix] for ix in np.where(clustered_labeled_embed.label == high_res_id_uniq_)[0]]),
                    dim=0)
                all_mean_culsters.append(mean_embedding.unsqueeze(0))
            all_mean_culsters = np.concatenate(all_mean_culsters)
            # N componenets are known for advance by the DBSCAN however hard positives may be clustered to an additional cluster !!
            gm = GaussianMixture(n_components=np.unique(clustered_labeled_embed.label).shape[0],
                                 random_state=0, covariance_type=self.covariance_type,
                                 init_params='k-means++', means_init=all_mean_culsters, verbose=1, verbose_interval=1)
            gm.fit(np.concatenate(([np.array(x)[np.newaxis, :] for x in clustered_labeled_embed.embed])))
            assert (gm.converged_)
            print(gm.means_.shape)

            if gm.covariance_type == 'diag':
                determinant = [np.linalg.det(np.diag(x)) for x in gm.covariances_]
            elif gm.covariance_type == 'full':
                determinant = np.linalg.det(gm.covariances_)
            if 0 in determinant:
                if any([np.sum(np.log(x)) >=0 for x in gm.covariances_]): # sum of log(EIG) too small
                    print("Huston cov matrix GMM determinant is Zero !!!")
                else:
                    print("Low Det low volume : ", [np.sum(np.log(x)) for x in gm.covariances_])
                    if gm.covariance_type == 'diag':
                        print("Covaraince Cond No. is ", [np.linalg.cond(np.diag(x)) for x in gm.covariances_])
            # Sanity check
            all_pred_centroid_score = list()
            all_pred_centroid_mean_score = list()
            for centroids_clusters, _ in enumerate(all_mean_culsters):
                pred_centroid = gm.predict(all_mean_culsters[centroids_clusters, :][np.newaxis, :])
                all_pred_centroid_score.append(gm.score(all_mean_culsters[centroids_clusters, :][np.newaxis, :]))
                all_pred_centroid_mean_score.append(gm.score(gm.means_[centroids_clusters, :][np.newaxis, :]))
                print("(Sanity) Pred centroid {} by GMM given the centroid of cluster {} score un normed {}".format(pred_centroid,
                                                                                                                    centroids_clusters, all_pred_centroid_score[-1]))
                if pred_centroid != centroids_clusters:
                    print("Heuston Pred centroid by GMM != the centroid of cluster!!!")
            if ~np.any([np.isclose(mu_gmm_score, avg_cluster) for (mu_gmm_score, avg_cluster) in zip(all_pred_centroid_mean_score, all_pred_centroid_score)]):
                print("One of the gaussians log likelihood at mean vector is different than @ cluster centroid", all_pred_centroid_mean_score, all_pred_centroid_score)
            # gm.predict(all_embeddings[28, :][np.newaxis, :])
            # score_dist = [gm.score(x.reshape(1, -1)) for x in all_embeddings]
            # print("llr distribution", score_dist)
            # norm_dist = np.max(score_dist)
            log_dx = 200
            llr_ref = np.log(0.01) - all_mean_culsters.shape[1] - log_dx # =(0.01)^512
            log_n_dim_multivariage_gaussian_dist_nor_factor = -0.5*np.sum(np.log(gm.covariances_[1, :]))-np.log(all_mean_culsters.shape[1]/2)+np.log(2*np.pi)

            for mdf, id_v in mdf_id_all.items():
                for mdf_id_name, id_in_mdf_dict in id_v.items():
                    # post DBSCAN KNN clustering not assigned while prob is high and bbox is sailence
                    if (id_in_mdf_dict['prob'] > self.min_prob_hard_pos_reassign and id_in_mdf_dict['id'] == -1) or collect_statistics_training:
                        bbox = id_in_mdf_dict['bbox']
                        id_box_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                        if id_box_area > self.min_res_hard_pos_reassign: # high confidence sailence
                            embed_ix_in_id_key = [ix for ix, x in enumerate(id_key_in_mdf) if mdf_id_name == x][0]
                            indices = gm.predict(all_embeddings[embed_ix_in_id_key].reshape(1, -1))
                            densities = gm.predict_proba(all_embeddings[embed_ix_in_id_key].reshape(1, -1)) #_estimate_log_prob_resp :  = CjNj(mu,cov)/sum[j]CjNj(mu,cov)
                            # llr = gm.score(all_embeddings[embed_ix_in_id_key].reshape(1, -1))
                            score = gm.score_samples(all_embeddings[embed_ix_in_id_key].reshape(1, -1)) #logsumexp(self._estimate_weighted_log_prob(X), axis=1)
                            most_probable_density = densities[0][indices[0]]
                            norm_llr_score_related_to_max_llr = score - all_pred_centroid_score[indices[0]]
                            # Cosine distance
                            cos_dist_to_gaussians = dict()
                            for centroids_clusters, _ in enumerate(gm.means_):#enumerate(all_mean_culsters):
                                test_embed = (all_embeddings[embed_ix_in_id_key].reshape(1, -1)).numpy()
                                # id_mean = all_mean_culsters[centroids_clusters, :][np.newaxis, :]
                                id_mean = gm.means_[centroids_clusters, :][np.newaxis, :]
                                cos_dist_to_gaussians[centroids_clusters] = 1 - np.abs(np.sum(test_embed * id_mean) / (np.linalg.norm(test_embed) * np.linalg.norm(id_mean)))

                            cos_dist_ind_min_dist, cos_dist_min_dist = min(cos_dist_to_gaussians.items(), key=lambda x: x[1])
                            if cos_dist_ind_min_dist != indices[0] and norm_llr_score_related_to_max_llr > llr_ref:
                                print("MDF {} [ID {} ] min cosine distance: {} gaussian mean {} is different than GMM assignment {}".
                                      format(mdf, mdf_id_name, cos_dist_min_dist, cos_dist_ind_min_dist, indices[0]))

                            log_dist_emb_from_mean = score - log_n_dim_multivariage_gaussian_dist_nor_factor
                            # print((log_dist_emb_from_mean, norm_llr_score_related_to_max_llr, llr_ref ))

                            if collect_statistics_training:
                                all_no_det_clip_key.append({'MDF': mdf, 'id': mdf_id_name, 'classified': id_in_mdf_dict['id'], 'log_dist_emb_from_mean': log_dist_emb_from_mean,
                                                            'norm_llr_score_related_to_max_llr': norm_llr_score_related_to_max_llr, 'llr_ref': llr_ref})
                            # Add Chi-Square Test Accept or Reject the Null Hypothesis testing
                            centered_sample = (all_embeddings[embed_ix_in_id_key].reshape(1, -1) - gm.means_[indices[0], :][
                                                                                                   np.newaxis, :])
                            mahalanobis_square = np.matmul(np.matmul(centered_sample, np.diag(gm.covariances_[indices[0], :])),
                                      np.transpose(centered_sample))
                            mahalanobis_dist = np.sqrt(mahalanobis_square) # TODO Hotelling's T-squared distribution : A one-sample Hotelling's T2 test

                            if norm_llr_score_related_to_max_llr > llr_ref and not (collect_statistics_training):# or most_probable_density>0.99:
                                print('GMM model assign to MDF: {} box {} ID :{} normalized log likelihood: {} TH: {}'.format(mdf, mdf_id_all[mdf][mdf_id_name]['bbox'], indices[0], norm_llr_score_related_to_max_llr, llr_ref))
                                mdf_id_all[mdf][mdf_id_name]['id'] = indices[0]
                                clustered_labeled_embed.embed.append(all_embeddings[embed_ix_in_id_key])
                                clustered_labeled_embed.label.append(indices[0])
            if collect_statistics_training:
                import pandas as pd
                df_all_no_det_clip_key = pd.DataFrame(all_no_det_clip_key)
                if os.path.exists(csv_file):
                    df_load = pd.read_csv(csv_file, index_col=False)
                    df_all_no_det_clip_key = df_all_no_det_clip_key.append(df_load)
                df_all_no_det_clip_key.to_csv(csv_file, index=False)

        except Exception as e:
            print(e)

        return
    # @ TODO : add divergence monitoring or limit the approach with minimal points percluster it is built in to 5 in long movies but for short clip it is 2 :When one has insufficiently many points per mixture, estimating the covariance matrices becomes difficult, and the algorithm is known to diverge and find solutions with infinite likelihood unless one regularizes the covariances artificially.
    def reassign_knn(self, mdf_id_all, clustered_labeled_embed, all_embeddings, id_key_in_mdf, knn):

        from sklearn.neighbors import NearestNeighbors
        nbrs = NearestNeighbors(n_neighbors=knn, p=2).fit(np.stack(clustered_labeled_embed.embed))  # knn with the already clustered  : the closiest is the embed itself
        for mdf, id_v in mdf_id_all.items():
            for mdf_id_name, id_in_mdf_dict in id_v.items():
                # post DBSCAN KNN clustering not assigned while prob is high and bbox is sailence
                if id_in_mdf_dict['prob'] > self.min_prob_hard_pos_reassign and id_in_mdf_dict['id'] == -1:
                    bbox = id_in_mdf_dict['bbox']
                    id_box_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                    if id_box_area > self.min_res_hard_pos_reassign: # high confidence sailence
                        embed_ix_in_id_key = [ix for ix, x in enumerate(id_key_in_mdf) if mdf_id_name == x][0]
                        distances, indices = nbrs.kneighbors(all_embeddings[embed_ix_in_id_key].reshape(1, -1))
                        # K-NN with the relevant labels of the already clustrered embed.
                        id_in_mdf_knn = [clustered_labeled_embed.label[x] for x in indices[0]]
                        lbl, c = np.unique(id_in_mdf_knn, return_counts=True)
                        if c[-1] >= np.ceil(knn/2): # Majority
                            print("Hard positive in {} classified as {} Voted {} times".format(mdf_id_name, lbl[-1], c[-1]))
                            mdf_id_all[mdf][mdf_id_name]['id'] = lbl[-1]
                            clustered_labeled_embed.embed.append(all_embeddings[embed_ix_in_id_key])
                            clustered_labeled_embed.label.append(lbl[-1])

                        # id_in_mdf_knn = [id_key_in_mdf[x] for x in indices[0]]
                        # for temp in id_in_mdf_knn:
                        #     record, id_key, mdf_key = [(val[temp], temp, key) for key, val in mdf_id_all.items() if temp in val][0]


        return

    def reid_process_movie(self, path_mdf, result_path_with_movie=None, save_results_to_db=False, **kwargs):
        save_res_pkl = kwargs.pop('save_res_pkl', False)
        input_type = kwargs.pop('input_type', None)

        if isinstance(path_mdf, list):# pipeline api
            path_mdf = os.path.dirname(path_mdf[0])

        if not (os.path.exists(path_mdf)):
            warnings.warn(
                "MDF path {} does not exist !!!! R U sure you've mounted the dataset".format(path_mdf))
            status = False
            return status, None, None, ""

        movie_name = os.path.basename(path_mdf)
        self.result_path_with_movie = result_path_with_movie
        if save_results_to_db:
            re_id_image_file_web_path = kwargs.pop('re_id_image_file_web_path')
            re_id_image_file_web_path = os.path.join(re_id_image_file_web_path, movie_name)

        if result_path_with_movie is None:
            self.result_path_with_movie = os.getenv('REID_RESULT_PATH', '/media/media/services') # default scratch folder for analysis

            if not (os.path.isdir(self.result_path_with_movie)):
                raise ValueError("{} Not mounted hence can not write to that folder ".format(self.result_path_with_movie))
            self.result_path_with_movie = os.path.join(self.result_path_with_movie, movie_name)

        result_path_good_resolution_faces, result_path = create_result_path_folders(self.result_path_with_movie, self.margin,
                                                                                    self.min_face_res,
                                                                                    self.re_id_method,
                                                                                    self.simillarity_metric)
        if save_results_to_db:
            _, re_id_image_file_web_path = create_result_path_folders(re_id_image_file_web_path, self.margin,
                                                                                    self.min_face_res,
                                                                                    self.re_id_method,
                                                                                    self.simillarity_metric)

        # plot_fn = self.plot_fn
        all_embeddings, mtcnn_cropped_image, names, mdf_id_all, status = self.extract_faces(path_mdf,
                                                                                            result_path_good_resolution_faces,
                                                                                            **kwargs)
        if not (status):
            return True, None, None, "" # have to return Tru otherwise workflow gradient pipeline gen exception

        # Sprint #4 too few MDFs
        no_mdfs = len(list(mdf_id_all.values())) # No of effective MDFs with detected faces
        if no_mdfs < (self.re_id_method['min_cluster_size']*self.id_to_mdf_ratio): # heuristic to determine what is the K-NN in case too few MDFs for sprint#4
            self.re_id_method['min_cluster_size'] = self.min_ids_per_cluster_sparse_mdf # for s5 demo int(min(max(no_mdfs / self.id_to_mdf_ratio, 2), self.re_id_method['min_cluster_size']))
            print("Too few MDFs/Key-frames for robust RE-ID reducing K-NN = {}".format(self.re_id_method['min_cluster_size']))
            self.re_id_method['cluster_threshold'] = self.re_id_method['cluster_threshold'] + self.delta_thr_sparse_mdfs
            self.re_id_method['cluster_threshold'] = float("{:.2f}".format(self.re_id_method['cluster_threshold']))

            print("Too few MDFs/Key-frames for robust RE-ID increasing epsilon/distance = {}".format(self.re_id_method['cluster_threshold']))
            result_path_good_resolution_faces, result_path = create_result_path_folders(result_path, self.margin,
                                                                                        self.min_face_res, self.re_id_method,
                                                                                        self.simillarity_metric)

            if save_results_to_db:
                _, re_id_image_file_web_path = create_result_path_folders(re_id_image_file_web_path, self.margin,
                                                                          self.min_face_res,
                                                                          self.re_id_method,
                                                                          self.simillarity_metric)
        if input_type != "image": # if it's movie and not a single image
            mdf_id_all, labeled_embed = self.re_identification(all_embeddings, mtcnn_cropped_image, names,
                                                                    mdf_id_all, result_path, self.simillarity_metric)

            # self._check_valid_fields(labeled_embed, mdf_id_all) # When phantom appears embeddings should be removed from list check if necessary

            if self.re_id_method['method'] == 'dbscan' or self.re_id_method['method'] == 'hdbscan':
                fname_strind_2nd = ['_recluster' if self.recluster_hard_positives else ''][0]
                fname_string = str(self.min_face_res) + '_' + str(self.prob_th_filter_blurr) + '_eps_' + str(self.re_id_method['cluster_threshold']) + '_KNN_' + str(self.re_id_method['min_cluster_size']) + '_dist_' + str(self.simillarity_metric) + fname_strind_2nd
                with open(os.path.join(result_path, 're-id_res_' + fname_string +'.pkl'), 'wb') as f:
                    pickle.dump(mdf_id_all, f)

                if save_res_pkl: # too much memory not needed now
                    with open(os.path.join(result_path, 'face-id_embeddings_embed_' + fname_string + '.pkl'), 'wb') as f1:
                        pickle.dump(labeled_embed.embed, f1)

                with open(os.path.join(result_path, 'face-id_embeddings_label_' + fname_string + '.pkl'), 'wb') as f1:
                    pickle.dump(labeled_embed.label, f1)

            else:
                raise

        else:
            labeled_embed = []

        re_id_result_path = os.path.join(result_path, 're_id')
        if os.path.exists(re_id_result_path) and os.path.isdir(re_id_result_path):
            shutil.rmtree(re_id_result_path)
        if re_id_result_path and not os.path.exists(re_id_result_path):
            os.makedirs(re_id_result_path)

        if self.use_actor_name_db:
            self.assign_actor_name_to_id_cluster(labeled_embed, mdf_id_all, names, all_embeddings, input_type)

        print("MDFs with reid marked on : {}".format(re_id_result_path))
        self.plot_id_over_mdf(mdf_id_all, result_path=re_id_result_path, path_mdf=path_mdf, plot_fn=self.plot_fn, plot_names=True)
        # if save_results_to_db:
        #     re_id_image_file_web_path = os.path.join(re_id_image_file_web_path, 're_id')
        if not self.use_actor_name_db:
            return status, re_id_result_path, mdf_id_all, ''
        else:
            return status, re_id_result_path, mdf_id_all, self.id_to_actor_name
            
    def _check_valid_fields(self, labeled_embed, mdf_id_all):
        n_ids = 0
        for mdf, id_v in mdf_id_all.items():
            for mdf_id_name, id_in_mdf_dict in id_v.items():
                # post DBSCAN KNN clustering not assigned while prob is high and bbox is sailence
                if id_in_mdf_dict['id'] != -1:
                    n_ids += 1
        assert(len(labeled_embed.label) == n_ids)

        return

    def plot_id_over_mdf(self, mdf_id_all, result_path, path_mdf, plot_fn=False, plot_names=True): # FN plot the IDs that weren't classified
        text_width, text_height = font.getsize('ID - 1')

        for file, ids_desc_all_clip_mdfs in tqdm.tqdm(mdf_id_all.items()):

            file_path = subprocess.getoutput('find ' + path_mdf + ' -iname ' + '"*' + file + '"') # handling nested folders
            if not file_path:
                print("File not found", file)
            if '\n' in file_path:
                file_path = file_path.split('\n')[0]

            # file_path = os.path.join(path_mdf, file)
            img = Image.open(file_path)
            img_draw = img.copy()
            draw = ImageDraw.Draw(img_draw)
            for ids, bbox_n_id in ids_desc_all_clip_mdfs.items():
                if ids_desc_all_clip_mdfs[ids]['id'] != -1:
                    box = ids_desc_all_clip_mdfs[ids]['bbox']
                    box[box < 0] = 0
                    box[3] = [img.size[1] if box[3] > img.size[1] else box[3]][0]
                    box[2] = [img.size[0] if box[2] > img.size[0] else box[2]][0]
                    draw.rectangle(box.tolist(), width=6, outline=color_space[ids_desc_all_clip_mdfs[ids]['id'] % len(color_space)]) # landmark plot
                    margin = np.ceil(0.05 * text_height)
                    cur_id = str(ids_desc_all_clip_mdfs[ids]['id'])
                    if not plot_names:
                        draw.text(
                            (box[0] + margin, box[3] - text_height - margin),
                            cur_id,
                            fill='yellow',
                            font=font)
                    else:
                        draw.text(
                        (box[0] + margin, box[3] - text_height - margin + 15),
                        cur_id + "   " + self.id_to_actor_name[int(cur_id)]['actor_name'],
                        fill='yellow',
                        font=font)
                elif plot_fn:
                    box = ids_desc_all_clip_mdfs[ids]['bbox']
                    # draw.rectangle(box.tolist(), width=10, outline=color_space[ids_desc_all_clip_mdfs[ids]['id'] % len(color_space)]) # landmark plot
                    draw.rounded_rectangle(box.tolist(), width=10, radius=10, outline=color_space[ids_desc_all_clip_mdfs[ids]['id'] % len(color_space)]) # landmark plot
                    margin = np.ceil(0.05 * text_height)
                    draw.text(
                        (box[0] + margin, box[3] - text_height - margin),
                        str(-1),
                        fill='yellow',
                        font=font)

            img_draw.save(os.path.join(result_path, 're-id_' + os.path.basename(file)))
        return


def calculateMahalanobis(y, inv_covmat, y_mu):
    y_mu = y - y_mu
    # if not cov:
    #     cov = np.cov(data.values.T)
    # inv_covmat = np.linalg.inv(cov)
    left = np.dot(y_mu, inv_covmat)
    mahal = np.dot(left, y_mu.T)
    return mahal.diagonal()

def create_result_path_folders(result_path, margin, min_face_res, re_id_method, simillarity_metric):

    result_path_good_resolution_faces = os.path.join(result_path, 'good_res')
    # result_path = os.path.join(result_path, 'res_' + str(min_face_res) + '_margin_' + str(margin) + '_eps_'  + str(args.cluster_threshold)) + '_KNN_' + str(re_id_method['min_cluster_size']) + '_' + str(args.simillarity_metric)
    result_path = os.path.join(result_path, 'method_' + str(re_id_method['method']) +'_res_' + str(min_face_res) + '_margin_' + str(margin) + '_eps_' + str(
        re_id_method['cluster_threshold'])) + '_KNN_' + str(re_id_method['min_cluster_size']) + '_dist_' + simillarity_metric

    if result_path and not os.path.exists(result_path):
        try:
            os.makedirs(result_path)
        except:
            os.system("sudo mkdir " + result_path)

    if result_path_good_resolution_faces and not os.path.exists(result_path_good_resolution_faces):
        os.makedirs(result_path_good_resolution_faces)
    return result_path_good_resolution_faces, result_path


def calculate_ap(annotation_path, mdf_face_id_all, result_path, movie_name):
    gt_vs_det, all_no_det_clip_key = parse_annotations_lsmdc(annotation_path, mdf_face_id_all, movie_name)

    most_common = dict()
    remove_prev_ids = list()
    for key, value in gt_vs_det.items():
        array_det_indecses = gt_vs_det[key][gt_vs_det[key] != -1]
        # remove previous most common from
        for rem_id in remove_prev_ids:
            array_det_indecses = array_det_indecses[array_det_indecses != rem_id]
        # array_det_indecses = [i[1][i[1] != -1] for i in gt_vs_det.items()][_gt_id]
        if any(array_det_indecses): # may be no assignment  : Counter([i[1][i[1] != -1] for i in gt_vs_det.items()][0])
            most_common_detected_index_assigned = Counter(array_det_indecses).most_common(1)[0][0] # take most frequent
            most_common.update({key: most_common_detected_index_assigned})
            remove_prev_ids.append(most_common_detected_index_assigned)

    for key, value in gt_vs_det.items(): #
        lsmdc_person_id = most_common.get(key)
        if lsmdc_person_id is not None:
            ratio_of_the_matching_labels = np.array((value == lsmdc_person_id)).astype('int').sum()/value.shape[0]
            print("LSMDC PERSON {} ratio of matching labels {}".format(lsmdc_person_id, ratio_of_the_matching_labels))
    # instance-level accuracy over ID pairs (“Inst-Acc”) as Trevor paper identity aware...
    """
Note, that it is important to
correctly predict both “Same ID” and “Different ID” labels, which can be seen
as a 2-class prediction problem. The instance-level accuracy does not distinguish
between these two cases. Thus, we introduce a class-level accuracy, where we
separately compute accuracy over the two subsets of ID pairs (“Same-Acc”,
“Diff-Acc”) and report the harmonic mean between the two (“Class-Acc”).
    """
    # n_classes = np.unique()
    # all_targets = list()
    # all_predictions = list()
    # p_r_plot_multi_class(all_targets, all_predictions, result_path, thresholds_every_in=5, unique_id=None,
    #                      classes=[*range(n_classes)])

    df_all_no_det_clip_key = pd.DataFrame(all_no_det_clip_key)
    df_all_no_det_clip_key.to_csv(os.path.join(result_path, 'fn_mdf_list.csv'), index=False)
    return


def parse_annotations_lsmdc(annotation_path, mdf_face_id_all, movie_name):
    df = pd.read_csv(annotation_path, index_col=False)  # , dtype={'id': 'str'})
    df['movie'] = df['clip'].apply(lambda x: "_".join(x.split('-')[0].split('.')[0].split('_')[:-1]))
    df = df[df.movie == movie_name]
    print("Total No of movies", len(df['movie'].unique()))
    gt_vs_det = dict() # collect the detected IDs only when single PERSONx GT exist,

    for movie in df['movie'].unique():
        no_detected_id_mdfs = 0
        all_no_det_clip_key = list()
        for clip in df['clip'][df.movie == movie]:
            # print("Movie /clip", movie, clip)

            mdf_keys_per_lsmdc_clip = [key for key, value in mdf_face_id_all.items() if clip.lower() in key.lower()]
            if not (mdf_keys_per_lsmdc_clip):
                Warning("No MDF was found although caption based annotations exist", movie, clip)
                continue
 # 1 LSMDC clip may contains few MDF which one of them may hold ID equivalent to GT in case more than 1 the same loop iterate multiple time but the final update will take according to the right MDF key
            predicted_per_clip = list()
            for clip_key in mdf_keys_per_lsmdc_clip:
                # Parse annotation from CSV can be single or multiple persons per clip = multiple MDFs
                # df['id'][df['clip']==clip]
                ids1 = df['id'][df['clip'] == clip].item()
                ids1 = ids1.replace('[', '')
                ids1 = ids1.replace(']', '')
                start_vec = [i.span() for i in re.finditer('person', ids1.lower())]  # [(), ()]
                if not (start_vec):
                    id_no_person_per_mdf = list() # empty dummy to skip the next merge
                    continue

                commas = [i.span() for i in re.finditer(',', ids1.lower())]
                commas.append((len(ids1), len(ids1)))  # dummy for last  seperator at the end of 2nd person

                id_no_person_per_mdf = list() # LSMDC one annotation of PERSONS per all CLIP no idea about MDFs
                for strt_stop, comma in zip(start_vec, commas):
                    # start = ids1.lower().find('person')
                    start = strt_stop[1]

                    # stop = strt_stop[0]
                    if start != -1:
                        id_no_person_per_mdf.append(int(ids1[start:comma[0]]))
                    else:
                        continue

                predicted_per_clip.extend([v['id'] for k, v in mdf_face_id_all[clip_key].items() if 'id' in v])
                mdf_face_id_all[clip_key].update({'gt_from_caption': id_no_person_per_mdf})
                mdf_face_id_all[clip_key].update({'clip_name': clip}) # add the common clip name shared among few MDF records
                # print(ids1, id_no_person_per_mdf)
# Merge : per lsmdc clip review predicted IDs to be uniqye in order not to solve bipartite matching
            if (not(predicted_per_clip) or all([x==-1 for x in predicted_per_clip])) and id_no_person_per_mdf:
                predicted_per_clip = np.array([not_id]) # class dummy means no ID at all
                print('FN !! CLIP: {} MDF : {} '.format(clip, clip_key))
                all_no_det_clip_key.append(clip_key)
                no_detected_id_mdfs += 1
            else:
                predicted_per_clip = np.unique(predicted_per_clip)
            if (predicted_per_clip.shape[0]> 1 or len(id_no_person_per_mdf)>1) or not(id_no_person_per_mdf):
                # print('No unique predicted IDs or GT PERSONS can not tell skip ')
                continue
            id_no_person_per_mdf = id_no_person_per_mdf[0]
            # Update dict mapping of PERSON GT to related detected IDs over all LSMDC clips
            if id_no_person_per_mdf in gt_vs_det: # for the first time indexing that gt
                predicted_per_clip = np.append(predicted_per_clip, gt_vs_det[id_no_person_per_mdf])
            gt_vs_det.update({id_no_person_per_mdf: predicted_per_clip})

        print("Missing IDs {} out of {} MDFs ratio: {}".format(no_detected_id_mdfs, len(mdf_face_id_all), no_detected_id_mdfs/len(mdf_face_id_all)))
        return gt_vs_det, all_no_det_clip_key


def facenet_embeddings(aligned, batch_size, image_size, device, neural_net):
# FaceNet create embeddings
    if isinstance(aligned, list):
        aligned = torch.stack(aligned)
    elif isinstance(aligned, torch.Tensor):
        pass
    else:
        raise
    if aligned.shape[0]%batch_size != 0: # all images size are Int multiple of batch size 
        pad = batch_size - aligned.shape[0]%batch_size
    else:
        pad = 0
    aligned = torch.cat((aligned, torch.zeros((pad, 3, image_size, image_size))), 0)
    all_embeddings = list()
    for frame_num in range(int(aligned.shape[0]/batch_size)):
        with torch.no_grad():
            if batch_size > 0:
                # ind = frame_num % batch_size
                batch_array = aligned[frame_num*batch_size:(frame_num+1)*batch_size, :,:,:]
                batch_array = batch_array.to(device)
                embeddings = neural_net(batch_array).detach().cpu()
                all_embeddings.append(embeddings)
            else:
                embeddings = neural_net(batch_array)
                all_embeddings.append(embeddings)
    all_embeddings = torch.cat(all_embeddings, 0) #np.concatenate(all_embeddings)
    all_embeddings = all_embeddings[:all_embeddings.shape[0]-pad,:]

    return all_embeddings

def find_key_given_value(clusters, ix):
    # [list(clusters.values())[0].tolist().index(ix)]
    for id, bbox_no in clusters.items():  # for name, age in dictionary.iteritems():  (for Python 2.x)
        if ix in bbox_no:
            return id 
    return -1




    if 0:
        sorted_clusters = _chinese_whispers(all_embeddings)
    # do UMAP/TSNe

def main():

# '3001_21_JUMP_STREET'--task metric_calc --cluster-threshold 0.3 --min-face-res 72 --min-cluster-size 6
    parser = ArgumentParser()
    parser.add_argument("--path-mdf", type=str, help="MVAD dataset path",  default='/home/hanoch/mdf_lsmdc/all')
    parser.add_argument("--movie", type=str, help="MVAD-Names dataset file path", default='0001_American_Beauty')#'3001_21_JUMP_STREET')#default='0001_American_Beauty')
    parser.add_argument("--result-path", type=str, help="", default='/media/results/face_reid')
    parser.add_argument('--batch-size', type=int, default=128, metavar='INT', help="TODO")
    parser.add_argument('--mtcnn-margin', type=int, default=40, metavar='INT', help="TODO")
    parser.add_argument('--min-face-res', type=int, default=96, metavar='INT', help="TODO")
    parser.add_argument('--cluster-threshold', type=float, default=0.27, metavar='FLOAT', help="TODO")
    parser.add_argument('--min-cluster-size', type=int, default=5, metavar='INT', help="TODO")
    parser.add_argument('--simillarity-metric', type=str, default='cosine',  # TODO not fully implemented
                        choices=['cosine', 'euclidean', 'mahalanobis'], metavar='STRING', help='') # distances https://hdbscan.readthedocs.io/en/latest/basic_hdbscan.html?highlight=distance#what-about-different-metrics

    parser.add_argument('--task', type=str, default='classify_faces', choices=['classify_faces', 'metric_calc', 'embeddings_viz_umap', 'plot_id_over_mdf'],
                        metavar='STRING', help='')

    parser.add_argument('--reid-method', type=str, default='dbscan', choices=['dbscan', 'hdbscan'],
                        metavar='STRING', help='')

    parser.add_argument('--plot-fn', action='store_true',
                         help='Plot False negatives over the reID image debug results')

    parser.add_argument('--save-res-pkl', action='store_true',
                         help='')

    parser.add_argument('--recluster-hard-positives', action='store_true',
                        help='recluster-hard-positives by K-NN')

    parser.add_argument("--annotation-path", type=str, help="",  default='/home/hanoch/notebooks/nebula3_reid/annotations/LSMDC16_annos_training_onlyIDs_NEW_local.csv')


    parser.add_argument('--media-type', type=str, default='video',  # TODO not fully implemented
                        choices=['video', 'image'], metavar='STRING',
                        help='')  # distances https://hdbscan.readthedocs.io/en/latest/basic_hdbscan.html?highlight=distance#what-about-different-metrics

    args = parser.parse_args()


    print_arguments(args)

    result_dict = dict()
    result_dict.update(vars(args))

    result_path_with_movie = os.path.join(args.result_path, args.movie)
    path_mdf = args.path_mdf
    path_mdf = os.path.join(path_mdf, args.movie)

    batch_size = args.batch_size
    margin = args.mtcnn_margin
    min_face_res = args.min_face_res#64 #+ margin #64*1.125 + margin # margin is post processing 
    prob_th_filter_blurr = 0.95
    batch_size = batch_size if torch.cuda.is_available() else 0
    re_id_method = {'method': args.reid_method, 'cluster_threshold': args.cluster_threshold, 'min_cluster_size': args.min_cluster_size}#0.4}

# if operational_mode:
#     print("!!!!!!!  For demo algorithm params were set to defaults  !!!!!!!!!!! R U Sure")
#     face_reid = FaceReId()
# else:
    face_reid = FaceReId(margin=margin, min_face_res=min_face_res,
                         re_id_method=re_id_method,
                         simillarity_metric=args.simillarity_metric,
                         prob_th_filter_blurr=prob_th_filter_blurr,
                         batch_size=batch_size,
                         plot_fn=args.plot_fn,
                         recluster_hard_positives=args.recluster_hard_positives)

    print("Settings margin :{} min_face_res{} re_id_method {} simillarity_metric {}".format(face_reid.margin,
                                                                                            face_reid.min_face_res, face_reid.re_id_method, face_reid.simillarity_metric))
    if args.task == 'classify_faces':
        success, re_id_result_path, _, id_to_actor_name = face_reid.reid_process_movie(path_mdf, result_path_with_movie,
                                                                                       input_type=args.media_type, save_res_pkl=args.save_res_pkl)
        if re_id_result_path  != None:
            fname_strind_2nd = ['_recluster' if face_reid.recluster_hard_positives else ''][0]
            fname_string = str(face_reid.min_face_res) + '_' + str(face_reid.prob_th_filter_blurr) + '_eps_' + str(face_reid.re_id_method['cluster_threshold']) + '_KNN_' + str(face_reid.re_id_method['min_cluster_size']) + '_dist_' + str(face_reid.simillarity_metric) + fname_strind_2nd

            import pandas as pd
            df_result = pd.DataFrame.from_dict(list(result_dict.items()))
            df_result = df_result.transpose()
            df_result['basic_classification_ratio'] = face_reid.basic_classification_ratio
            df_result['reassign_classification_ratio'] = face_reid.reassign_classification_ratio

            df_result.to_csv(os.path.join(os.path.dirname(re_id_result_path), 'setup_' + fname_string +'.csv'), index=False)

        print("success : ", success)
        return success

    elif args.task == 'metric_calc':
        result_path_good_resolution_faces, result_path = create_result_path_folders(result_path_with_movie, face_reid.margin,
                                                                                    face_reid.min_face_res,
                                                                                    face_reid.re_id_method)

        fname_strind_2nd = ['_recluster' if face_reid.recluster_hard_positives else ''][0]

        fname_string = str(face_reid.min_face_res) + '_' + str(face_reid.prob_th_filter_blurr) + '_eps_' + str(
            face_reid.re_id_method['cluster_threshold']) + '_KNN_' + str(face_reid.re_id_method['min_cluster_size']) + '_dist_' + str(face_reid.simillarity_metric) + fname_strind_2nd

        with open(os.path.join(result_path, 're-id_res_' + fname_string + '.pkl'), 'rb') as f:
            mdf_face_id_all = pickle.load(f)

        ap = calculate_ap(args.annotation_path, mdf_face_id_all, result_path, args.movie)

    elif args.task == 'embeddings_viz_umap':
        _, result_path = create_result_path_folders(result_path_with_movie, face_reid.margin,
                                                                                face_reid.min_face_res,
                                                                                face_reid.re_id_method,
                                                                                face_reid.simillarity_metric)
        labeled_embed = EmbeddingsCollect()

        fname_strind_2nd = ['_recluster' if face_reid.recluster_hard_positives else ''][0]
        fname_string = str(face_reid.min_face_res) + '_' + str(face_reid.prob_th_filter_blurr) + '_eps_' + str(
            face_reid.re_id_method['cluster_threshold']) + '_KNN_' + str(face_reid.re_id_method['min_cluster_size']) + '_dist_' + str(face_reid.simillarity_metric) + fname_strind_2nd

        with open(os.path.join(result_path, 'face-id_embeddings_embed_' + fname_string + '.pkl'), 'rb') as f:
            labeled_embed.embed = pickle.load(f)

        with open(os.path.join(result_path, 'face-id_embeddings_label_' + fname_string + '.pkl'), 'rb') as f1:
            labeled_embed.label = pickle.load(f1)

        with open(os.path.join(result_path, 're-id_res_' + fname_string + '.pkl'), 'rb') as f:
            mdf_face_id_all = pickle.load(f)  #@HK TODO associate the entries/MDF in this dict to the embedings/labels to compute bbox area

        print("similarity based UMAP", args.simillarity_metric)


        if args.plot_fn:
            import pandas as pd
            path_fn = '/home/hanoch/results/face_reid/face_net/0001_American_Beauty/res_64_margin_40_eps_0.27_KNN_5/FN/FN_0001_American_Beauty.csv'#'/home/hanoch/notebooks/nebula3_reid/annotations/FN_0001_American_Beauty.csv'
            df = pd.read_csv(path_fn, index_col=False)  # , dtype={'id': 'str'})
            df.dropna(axis='columns')
            mdf_path = '/home/hanoch/results/face_reid/face_net/0001_American_Beauty/res_64_margin_40_eps_0.27_KNN_5/FN/mdf' #'/home/hanoch/results/face_reid/face_net/0001_American_Beauty/fn'
            id_fn_all = np.unique(df['id']) #1 # 1 = kavin
            print("FN IDs is ", id_fn_all)
            if 0: # create mdf related database
                dest_path = mdf_path #'/home/hanoch/results/face_reid/face_net/0001_American_Beauty/fn'
                import subprocess
                if path_mdf is None:
                    raise
                for id_fn in id_fn_all:
                    for reid_fname in df[df['id'] == id_fn]['mdf']:  # Kevin Spacy
                        fname = reid_fname.split('re-id_')[-1]
                        file_full_path = subprocess.getoutput('find ' + path_mdf + ' -iname ' + '"*' + fname + '*"')
                        if not file_full_path:
                            print("File not found", fname)
                        subprocess.getoutput('cp -p ' + file_full_path + ' ' + dest_path)

            result_path_fn = os.path.join(mdf_path, 'face_rec')
            if result_path_fn and not os.path.exists(result_path_fn):
                os.makedirs(result_path_fn)
            # Extract embeddinegs for the FN cases
            all_embeddings, mtcnn_cropped_image, names, mdf_id_all, status = face_reid.extract_faces(path_mdf=mdf_path,
                                                                                    result_path_good_resolution_faces=result_path_fn,
                                                                                    plot_cropped_faces=True)

            print("Embeddings of all reID ", len(labeled_embed.embed))

            un_clustered_embd = list()
            un_clustered_lbl = list()
            for ix in range(len(names)):
                if np.array([names[ix].split(args.movie)[1] in na for na in names]).astype('int').sum() != 1:
                    raise ValueError("More than one face per MDF")
            # Add the unclassified ID
            fn_unique_label = np.sort(np.unique(labeled_embed.label))[-1] + 1
            for ix in range(all_embeddings.shape[0]):
                mdf_name_in_csv_loc = np.where([names[ix].split(args.movie)[1] in mdf for mdf in df.mdf])[0]
                if mdf_name_in_csv_loc.shape[0] != 1:
                    raise
                mdf_name_in_csv_loc = mdf_name_in_csv_loc.item()
                id_class = df.loc[mdf_name_in_csv_loc, 'id']

                un_clustered_embd.append(all_embeddings[ix])
                un_clustered_lbl.append(id_class)

                if 1:# dummy class appended to draw UMAP in order to differentiate FN embed from clustered
                    labeled_embed.embed.append(all_embeddings[ix])
                    labeled_embed.label.append(fn_unique_label + id_class)
                    print('Unclustered example added with ID {} aliased to class {}'.format(fn_unique_label + id_class, id_class))
                else:# true class from csv
                    labeled_embed.embed.append(all_embeddings[ix])
                    labeled_embed.label.append(id_class)
            # pairwise_similarity = np.matmul(matrix[np.nonzero(db.labels_==0)[0], :],matrix[np.nonzero(db.labels_==0)[0], :].T)

        # Collect statistics of each ID
        all_centroid = list()
        all_inv_cov = list()
        # test_embed = all_embeddings.cpu().numpy()
        for id_ix in np.sort(np.unique(labeled_embed.label)):
            lbl_ix = np.where([np.array(labeled_embed.label) == id_ix])[1]
            id_embed = [labeled_embed.embed[i] for i in lbl_ix]
            id_mean = np.mean(np.stack(id_embed), axis=0)

            all_centroid.append(id_mean)
            cov = np.cov(np.stack(id_embed).T)
            inv_convmat = np.linalg.inv(cov)

            all_inv_cov.append(inv_convmat)
            # dist_euc = np.linalg.norm(test_embed - id_mean)
            # cos_dist = 1 - np.sum(test_embed*id_mean)/(np.linalg.norm(test_embed) * np.linalg.norm(id_mean))

            # Metric learning assignment
            for uncluster_id in np.unique(un_clustered_lbl):
                maha = calculateMahalanobis(y=np.stack(un_clustered_embd)[un_clustered_lbl==uncluster_id, :],
                                            inv_covmat=inv_convmat, y_mu=id_mean)

            print("total embeddings of all reID and FN ", len(labeled_embed.embed))
            # FN class Already known no need for re-id
            result_path = result_path_fn
            if 0:
                with open(os.path.join(result_path, 'face-id_embeddings_embed_' + str(min_face_res) + '_' + str(prob_th_filter_blurr) + '_eps_' + str(re_id_method['cluster_threshold']) + '_KNN_'+ str(re_id_method['min_cluster_size']) + '.pkl'), 'wb') as f1:
                    pickle.dump(labeled_embed.embed, f1)

                with open(os.path.join(result_path, 'face-id_embeddings_label_' + str(min_face_res) + '_' + str(prob_th_filter_blurr) + '_eps_' + str(re_id_method['cluster_threshold']) + '_KNN_'+ str(re_id_method['min_cluster_size']) + '.pkl'), 'wb') as f1:
                    pickle.dump(labeled_embed.label, f1)


        umap_plot(labeled_embed, result_path, metric=args.simillarity_metric)

    elif args.task == 'plot_id_over_mdf':
        import subprocess
        file_full_path = subprocess.getoutput('find ' + args.result_path + ' -iname ' + '"*re-id_res_*"')
        with open(file_full_path, 'rb') as f:
            mdf_face_id_all = pickle.load(f)

        if 1:
            re_id_result_path = os.path.join(args.result_path, 're_id_fn')

            if re_id_result_path and not os.path.exists(re_id_result_path):
                os.makedirs(re_id_result_path)

            face_reid.plot_id_over_mdf(mdf_face_id_all, result_path=re_id_result_path, path_mdf=path_mdf, plot_fn=args.plot_fn)
        else:
            re_id_result_path = os.path.join(args.result_path, 're_id')
            face_reid.plot_id_over_mdf(mdf_face_id_all, result_path=re_id_result_path, path_mdf=path_mdf)




if __name__ == '__main__':
    main()


"""
--task classify_faces --path-mdf /media/mpii_reid/bbox/frames --task classify_faces --min-face-res 96 --min-cluster-size 5 --movie 1030_Public_Enemies --mtcnn-margin 20 --plot-fn --reid-method dbscan --cluster-threshold 0.27 --recluster-hard-positives
--task classify_faces --path-mdf /media/mdf_hollywood2/all --task classify_faces --min-face-res 96 --min-cluster-size 5 --movie actioncliptest00007 --mtcnn-margin 40 --plot-fn --reid-method dbscan --cluster-threshold 0.27
--task classify_faces --path-mdf /media/mdf_hollywood2/all --task classify_faces --min-face-res 96 --min-cluster-size 5 --movie actioncliptest00347 --mtcnn-margin 40 --plot-fn --reid-method dbscan --cluster-threshold 0.27 --result-path /media/results/face_reid/hollywood2
actioncliptest00007
--task classify_faces --cluster-threshold 0.26 --min-face-res 128 --min-cluster-size 5 --movie 0011_Gandhi --mtcnn-margin 40
sprint4
--path-mdf /media/media/frames --task classify_faces --cluster-threshold 0.27 --min-face-res 64 --min-cluster-size 5 --movie 0001_American_Beauty --mtcnn-margin 40 --result-path /media/media/services
--path-mdf /media/media/frames --task plot_id_over_mdf --cluster-threshold 0.37 --min-face-res 64 --min-cluster-size 5 --movie 0001_American_Beauty --mtcnn-margin 40 --result-path /media/media/services/0001_American_Beauty/res_64_margin_40_eps_0.27_KNN_5/res_64_margin_40_eps_0.42_KNN_2
--path-mdf /media/media/frames --task classify_faces --cluster-threshold 0.27 --min-face-res 64 --min-cluster-size 5 --movie video988 --mtcnn-margin 40 --result-path /media/media/services

--path-mdf /media/media/frames --task classify_faces --movie aVgeJ5eqlSM --result-path /media/media/services
/media/media/frames/aVgeJ5eqlSM
--path-mdf /media/media/frames --task classify_faces --movie 7-6_Ulo7mdk --result-path /media/media/services --recluster-hard-positives
--path-mdf /media/media/frames --task classify_faces --movie actioncliptest00013 --result-path /media/media/services --recluster-hard-positives

--path-mdf /media/media/movies/chinese_characters --task classify_faces --movie Full-River-Red.jpg --media-type image --recluster-hard-positives --min-face-res 32 --cluster-threshold 0.27

yjRHZEUamCc

# 0.47 WAS GREAT 
--task embeddings_viz_umap --cluster-threshold 0.27 --min-face-res 64 --min-cluster-size 5 --movie 0001_American_Beauty --mtcnn-margin 40
--task classify_faces --cluster-threshold 0.27 --min-face-res 64 --min-cluster-size 5 --movie 0001_American_Beauty --mtcnn-margin 40
--task metric_calc --cluster-threshold 0.28 --min-face-res 64 --min-cluster-size 5 --movie 0001_American_Beauty
--task metric_calc --cluster-threshold 0.3 --min-face-res 64 --min-cluster-size 5 --movie '3001_21_JUMP_STREET'
--task classify_faces --cluster-threshold 0.28 --min-face-res 64 --min-cluster-size 5
venv :
source /home/hanoch/.virtualenvs/nebula3_reid/bin/activate
source /home/hanoch/.virtualenvs/nebula3_reid/bin/activate

                    # import torch.cuda.profiler as profiler
                    # import pyprof
                    # pyprof.init()
                    # with torch.autograd.profiler.emit_nvtx():
                    #     profiler.start()
                    #     batch_boxes, prob, lanmarks_points = self.mtcnn.detect(img, landmarks=True)
                    #     x_aligned = self.mtcnn.extract(img, batch_boxes, save_path=None)  # implicitly saves the faces
                    #     profiler.stop()

"""



