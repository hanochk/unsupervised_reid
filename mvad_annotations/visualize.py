from __future__ import absolute_import, division, print_function

import numpy as np
import cv2
import os
import io
import argparse
import imageio
import matplotlib.cm as cm

try:
    import cPickle as pickle
except ImportError:
    import pickle


def get_video_frames(filename, mode='RGB'):
    try:
        video = imageio.get_reader(filename, 'ffmpeg')
    except Exception as e:
        print('Error: ', e)
        return False, None, None

    frame = video.get_data(0)
    width = frame.shape[1]
    height = frame.shape[0]
    n_frames = video.get_meta_data()['nframes']
    if np.isinf(n_frames) or np.isnan(n_frames) or n_frames is None:
        cap = cv2.VideoCapture(filename)
        n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    frame = video.get_data(0)
    video_properties = dict({
        'width': frame.shape[1],
        'height': frame.shape[0],
        'meta_data': video.get_meta_data(),
    })

    i = 0
    frames = np.zeros((n_frames, height, width, 3), dtype=np.uint8)
    try:
        for t in range(n_frames):
            if mode == 'RGB':
                frame = video.get_data(t)
            elif mode == 'BGR':
                frame = video.get_data(t)[:, :, ::-1]
            else:
                raise Exception('Unsopported mode. Mode can be RGB or BGR.')
            frames[t] = frame
            i += 1
    except imageio.core.format.CannotReadFrameError:
        print('Error. Only %d frames read out of %d' % (i, n_frames))
        frames = frames[:i]
    except Exception as e:
        print('Error: ', e)
        return False, frames, video_properties

    return True, frames, video_properties


def main():
    #             "args": ["--mvad_path", "/mnt/share", "--save_results", "--mvad_names_path", "/home/hanoch/notebooks/nebula3_reid/mvad_annotations/mvad-names.pkl"],
    parser = argparse.ArgumentParser()
    parser.add_argument("--mvad_path", type=str, help="MVAD dataset path", required=True)
    parser.add_argument("--mvad_names_path", type=str, help="MVAD-Names dataset file path", default='mvad-names.pkl')
    parser.add_argument("--movie", type=str, help="Name of the movie to process.", default=None)
    parser.add_argument("--clip", type=str, help="Clip IDs (split by space).", default=None)
    parser.add_argument("--save_results", action='store_true',
                        help="Save the output images in `./output` in place of visualizing them.")
    args = parser.parse_args()

    movie_folder = args.mvad_path
    dataset_file = args.mvad_names_path
    output_folder = './output'

    assert os.path.exists(movie_folder)
    assert os.path.exists(dataset_file)

    movies = os.listdir(movie_folder)

    if args.movie is not None:
        movies = [args.movie, ]
    if 1:
        movies = movies[2:]

    for movie in movies:
        assert os.path.exists(os.path.join(movie_folder, movie))

    if args.clip is not None:
        clips = args.clip.split(' ')
    else:
        clips = None

    color_map = (cm.gist_rainbow(np.linspace(0, 1, 7))[:, :-1] * 255)[:, ::-1].astype(np.uint8)

    print('-- MVAD-Names visualization demo --')

    with io.open(dataset_file, 'rb') as fd:
        ds = pickle.load(fd)

    if args.save_results and not os.path.exists(output_folder):
        os.makedirs(output_folder)
    # remove trailing number
    movies_no_prefix = ['_'.join(mov.split('_')[1:]) for mov in movies]
    for ix, (movie_no_prefix, movie) in enumerate(zip(movies_no_prefix, movies)):
        # movie = os.path.join(movie_folder, movie)
        print('\nMovie: %s' % movie)
        if not os.path.isdir(os.path.join(movie_folder, movie)):
            continue
        if not np.array([movie_no_prefix in i[0] for i in ds.items()]).any() or movie_no_prefix == '':
            continue 
        if 'TITANIC' in movie: # has 2 folders in MVAD but 1 in LSMDC
            continue 
        if '2012' in movie:  #SPARKLE_2012
            continue
        for clip in os.listdir(os.path.join(movie_folder, movie)): #[i[1].keys() for i in ds['BIG_MOMMAS_LIKE_FATHER_LIKE_SON']['characters'].items()]
            vid = clip[clip.rfind('_') + 1:-4]
            if 1: #'.avi' in clip and vid in ds[movie_no_prefix]['videos'].keys():
                if clips is not None:
                    if clip.split('_')[-1][3:-4] not in clips:
                        continue

                print('\tClip: %s' % clip)

                ret, frames, video_properties = get_video_frames(os.path.join(movie_folder, movie, clip), mode='BGR')
                if not ret:
                    continue

                for i, frame in enumerate(frames):
                    img = frame.copy()
                    if 1:
                        vid = 'DVS37'
                    for char_index, (c, char) in enumerate(ds[movie_no_prefix]['videos'][vid].items()):
                        for k, track in enumerate(char):
                            for bb in [x for x in track if x[0] == i]:
                                color = color_map[char_index % len(color_map)]
                                color = (int(color[0]), int(color[1]), int(color[2]))
                                img = cv2.rectangle(img, (bb[1], bb[2]), (bb[3], bb[4]), color)
                                cv2.putText(img, c, (bb[1], bb[2]), cv2.FONT_HERSHEY_PLAIN, 1, color, 1,
                                            cv2.QT_FONT_NORMAL, 0)
                    if not args.save_results:
                        cv2.imshow('MVAD-Names', img)
                        cv2.waitKey(42)
                    else:
                        cv2.imwrite(os.path.join(output_folder, '%s_%04d.jpg' % (clip, i)), img)


if __name__ == '__main__':
    main()
