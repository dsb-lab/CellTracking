from re import A
from cellpose.io import imread
from cellpose import models
import os
from CellTracking import *
import itertools

pth='/home/pablo/Desktop/PhD/projects/Data/blastocysts/movies/2h_claire_ERK-KTR_MKATE2/registered/'
files = os.listdir(pth)

emb = 10
modelpth = '/home/pablo/Desktop/PhD/projects/Data/blastocysts/movies/2h_claire_ERK-KTR_MKATE2/cell_tracking/training_set_expanded_nuc/models/blasto'
IMGS   = [imread(pth+f)[:,:,1,:,:] for f in files[emb:emb+1]]
model  = models.CellposeModel(gpu=True, pretrained_model=modelpth)
#model  = models.Cellpose(gpu=True, model_type='nuclei')

t = 1
imgs = IMGS[0][t,:,:,:]

CS = CellSegmentation( imgs, model, trainedmodel=True
                     , channels=[0,0]
                     , flow_th_cellpose=0.4
                     , distance_th_z=3.0
                     , xyresolution=0.2767553
                     , relative_overlap=False
                     , use_full_matrix_to_compute_overlap=True
                     , z_neighborhood=2
                     , overlap_gradient_th=0.15
                     , plot_layout=(1,2)
                     , plot_overlap=1
                     , plot_masks=False
                     , masks_cmap='tab10'
                     , min_outline_length=200
                     , neighbors_for_sequence_sorting=7)
CS()


class IndexTracker(object):
    def __init__(self, ax, X):
        self.ax = ax
        ax.set_title('use scroll wheel to navigate images')

        self.X = X
        rows, cols, self.slices = X.shape
        self.ind = self.slices//2

        self.im = ax.imshow(self.X[:, :, self.ind])
        self.update()

    def onscroll(self, event):
        print("%s %s" % (event.button, event.step))
        if event.button == 'up':
            self.ind = (self.ind + 1) % self.slices
        else:
            self.ind = (self.ind - 1) % self.slices
        self.update()

    def update(self):
        self.im.set_data(self.X[:, :, self.ind])
        self.ax.set_ylabel('slice %s' % self.ind)
        self.im.axes.figure.canvas.draw()


fig, ax = plt.subplots(1, 1)

X = np.random.rand(20, 20, 40)

tracker = IndexTracker(ax, X)


fig.canvas.mpl_connect('scroll_event', tracker.onscroll)
plt.show()