import numpy as np
import math
from cellpose import utils as utilscp
import matplotlib.pyplot as plt
import re
import matplotlib as mtp
from matplotlib import cm
import itertools
import random
from scipy.spatial import cKDTree
from copy import deepcopy
from matplotlib.widgets import Slider

plt.rcParams.update({'figure.max_open_warning': 0})

LINE_UP = '\033[1A'
LINE_CLEAR = '\x1b[2K'

# This class segments the cell of an embryo in a given time. The input data should be of shape (z, x or y, x or y)
class CellSegmentation(object):

    def __init__(self, stack, model, trainedmodel=None, channels=[0,0], flow_th_cellpose=0.4, distance_th_z=3.0, xyresolution=0.2767553, relative_overlap=False, use_full_matrix_to_compute_overlap=True, z_neighborhood=2, overlap_gradient_th=0.3, plot_layout=(2,2), plot_overlap=1, plot_masks=True, masks_cmap='tab10', min_outline_length=150, neighbors_for_sequence_sorting=7):
        self.stack               = stack
        self._model              = model
        self._trainedmodel       = trainedmodel
        self._channels           = channels
        self._flow_th_cellpose   = flow_th_cellpose
        self._distance_th_z      = distance_th_z
        self._xyresolution       = xyresolution
        self.slices              = self.stack.shape[0]
        self.stack_dims          = self.stack.shape
        self._relative           = relative_overlap
        self._fullmat            = use_full_matrix_to_compute_overlap
        self._zneigh             = z_neighborhood
        self._overlap_th         = overlap_gradient_th # is used to separed cells that could be consecutive on z
        self._actions            = [self.plot_segmented, self.update_labels, self.__call__, self._end_actions]
        self.plot_layout         = plot_layout
        self.plot_overlap        = plot_overlap
        self.plot_masks          = plot_masks
        self._max_label          = 0
        self._masks_cmap_name    = masks_cmap
        self._masks_cmap         = cm.get_cmap(self._masks_cmap_name)
        self._masks_colors       = self._masks_cmap.colors
        self._min_outline_length = min_outline_length
        self._nearest_neighs     = neighbors_for_sequence_sorting
        self._returnfalg         = False
        self._assign_color_to_label()

    def __call__(self):
        print("####################   SEGMENTING CURRENT EMBRYO  ####################")
        self.printfancy("")
        self._cell_segmentation_outlines()
        self.printfancy("")
        self.printfancy("Raw segmentation completed")
        self._update()

        self.printfancy("Running segmentation post-processing...")
        self._separate_concatenated_cells()
        self._update()

        self._separate_concatenated_cells()
        self._update()

        self._remove_short_cells()
        self._update()
        self._position3d()
        self.printfancy("")

        self.printfancy("Segmentation completed and revised")
        self.printfancy("")
        print("####################     SEGMENTATION COMPLETED   ####################")
        self.printfancy("")
        self.actions()

    def _cell_segmentation_outlines(self):

        # This function will return the Outlines and Mask of the current embryo. 
        # The structure will be (z, cell_number)
        self.Outlines = []
        self.Masks    = []

        # Number of z-levels
        
        self.printfancy("Progress: ")
        # Loop over the z-levels
        for z in range(self.slices):
            self.progress(z+1, self.slices)
            # Current xy plane
            img = self.stack[z,:,:]

            # Select whether we are using a pre-trained model or a cellpose base-model
            if self._trainedmodel:
                masks, flows, styles = self._model.eval(img)
            else:
                masks, flows, styles, diam = self._model.eval(img, channels=self._channels, flow_threshold=self._flow_th_cellpose)
            
            # Extract the oulines from the masks using the cellpose function for it. 
            outlines = utilscp.outlines_list(masks)

            # Append the empty masks list for the current z-level.
            self.Masks.append([])

            # We now check which oulines do we keep and which we remove.
            idxtoremove = []
            for cell, outline in enumerate(outlines):
                outlines[cell] = self._increase_point_resolution(outline)

                # Compute cell mask
                ptsin = self._points_within_hull(outlines[cell])

                # Check for empty masks and keep the corresponding cell index. 
                if len(ptsin)==0:
                    idxtoremove.append(cell)

                # Store the mask otherwise
                else:
                    self.Masks[z].append(ptsin)

            # Remove the outlines for the masks
            for idrem in idxtoremove:
                outlines.pop(idrem)

            # Keep the ouline for the current z-level
            self.Outlines.append(outlines)

    def _points_within_hull(self, hull):
        # With this function we compute the points contained within a hull or outline.
        pointsinside=[]
        sortidx = np.argsort(hull[:,1])
        outx = hull[:,0][sortidx]
        outy = hull[:,1][sortidx]
        curry = outy[0]
        minx = np.iinfo(np.int32).max
        maxx = 0
        for j,y in enumerate(outy):
            done=False
            while not done:
                if y==curry:
                    minx = np.minimum(minx, outx[j])
                    maxx = np.maximum(maxx, outx[j])
                    done=True
                    curry=y
                else:
                    for x in range(minx, maxx+1):
                        pointsinside.append([x, curry])
                    minx = np.iinfo(np.int32).max
                    maxx = 0
                    curry= y

        pointsinside=np.array(pointsinside)
        return pointsinside
    
    def _extract_cell_centers(self):
        # Function for extracting the cell centers for the masks of a given embryo. 
        # It is extracted computing the positional centroid weighted with the intensisty of each point. 
        # It returns list of similar shape as Outlines and Masks. 
        self.centersi = []
        self.centersj = []

        # Loop over each z-level
        for z, outlines in enumerate(self.Outlines):

            # Current xy plane with the intensity of fluorescence 
            img = self.stack[z,:,:]

            # Append an empty list for the current z-level. We will push here the i and j coordinates of each cell. 
            self.centersi.append([])
            self.centersj.append([])

            # Loop over all the cells detected in this level
            for cell, outline in enumerate(outlines):

                # x and y coordinates of the centroid.
                xs = np.average(self.Masks[z][cell][:,1], weights=img[self.Masks[z][cell][:,1], self.Masks[z][cell][:,0]])
                ys = np.average(self.Masks[z][cell][:,0], weights=img[self.Masks[z][cell][:,1], self.Masks[z][cell][:,0]])
                self.centersi[z].append(xs)
                self.centersj[z].append(ys)

    def _compute_distances_with_pre_post_z(self):
        self._distances_idx = []
        self._distances_val = []
        distance_th = np.round(self._distance_th_z/self._xyresolution)
        for z in range(self.slices):
            self._distances_idx.append([])
            self._distances_val.append([])
            for cell in range(len(self.centersi[z])):
                self._distances_idx[z].append([])
                self._distances_val[z].append([])
                poscell = np.array([self.centersi[z][cell], self.centersj[z][cell]])
                self._distances_idx[z][cell].append([])
                self._distances_idx[z][cell].append([])
                self._distances_val[z][cell].append([])
                self._distances_val[z][cell].append([])
                if z>0:
                    for cellpre,_ in enumerate(self.centersi[z-1]):
                        poscell2 = np.array([self.centersi[z-1][cellpre], self.centersj[z-1][cellpre]])    
                        dist = np.linalg.norm(poscell-poscell2)
                        if dist < distance_th:
                            self._distances_idx[z][cell][0].append(cellpre)
                            self._distances_val[z][cell][0].append(dist)
                if z<self.slices-1:
                    for cellpost,_ in enumerate(self.centersi[z+1]):
                        poscell2 = np.array([self.centersi[z+1][cellpost], self.centersj[z+1][cellpost]])           
                        dist = np.linalg.norm(poscell-poscell2)
                        if dist < distance_th:
                            self._distances_idx[z][cell][1].append(cellpost)
                            self._distances_val[z][cell][1].append(dist)    

    def _assign_labels(self):
        self.labels=[]
        last_label=None
        used_labels = []
        for z in range(self.slices):
            self.labels.append([])
            current_labels=[]
            current_labels_cell=[]
            for cell, outline in enumerate(self.Outlines[z]):
                
                # If this is the first plane, start filling
                if z==0:
                    if last_label==None:
                        label=0
                    else:
                        label=last_label+1
                
                # Otherwise do the magic
                elif z>0:
                    if last_label==None:
                        label=0
                    else:
                        if len(self._distances_val[z][cell][0])== 0:
                            label=last_label+1
                        else:
                            idx_closest_cell = self._distances_idx[z][cell][0][np.argmin(self._distances_val[z][cell][0])]
                            label = self.labels[z-1][idx_closest_cell]
                            if label in current_labels:
                                curr_dist  = np.min(self._distances_val[z][cell][0])
                                idx_other  = np.where(current_labels==label)[0][0]
                                close_cells = True

                                if len(self._distances_val[z][idx_other])==0:
                                    close_cells=False
                                else:
                                    if len(self._distances_val[z][idx_other][0])==0:
                                        close_cells=False
                                
                                if close_cells:
                                    other_dist = np.min(self._distances_val[z][idx_other][0])
                                    if curr_dist<other_dist:
                                        current_labels[idx_other]=last_label+1
                                        self.labels[z][idx_other]=last_label+1
                                    else:
                                        label = last_label+1
                                else:
                                    current_labels[idx_other]=last_label+1
                                    self.labels[z][idx_other]=last_label+1

                used_labels.append(label)
                current_labels.append(label)
                current_labels_cell.append(cell)
                last_label=np.max(used_labels)
                self.labels[z].append(label)

    def _label_per_z(self):
        # Data re-structuring to correct possible alignment of contiguous cells along the z axis. 
        self._Zlabel_l = []
        self._Zlabel_z = []
        for z in range(30):
            for l in self.labels[z]:
                if l not in self._Zlabel_l:
                    self._Zlabel_l.append(l)
                    self._Zlabel_z.append([])
                id = self._Zlabel_l.index(l)
                self._Zlabel_z[id].append(z)

    def _remove_short_cells(self):
        self._label_per_z()
        labels_to_remove = []
        for id, l in enumerate(self._Zlabel_l):        
            if len(self._Zlabel_z[id]) < 2: # Threshold for how many planes a cell has to be to be considered
                labels_to_remove.append(l)
        for z, labs in enumerate(self.labels):
            for l in labels_to_remove:
                if l in labs:
                    id_l=labs.index(l)
                    self.labels[z].pop(id_l)
                    self.Outlines[z].pop(id_l)
                    self.Masks[z].pop(id_l)

    def _nuclear_intensity_cell_z(self):
        self._label_per_z()
        self._Zsignals = []
        for id, l in enumerate(self._Zlabel_l):
            # Compute nucleus intensity if num of z is greater than another threshold
            if len(self._Zlabel_z[id]) > 0:
                self._Zsignals.append([])
                for z in self._Zlabel_z[id]:
                    id_l = self.labels[z].index(l)
                    img  = self.stack[z,:,:]#/np.max(self.stack[z,:,:])
                    mask = self.Masks[z][id_l]
                    self._Zsignals[-1].append(np.sum(img[mask[:,1], mask[:,0]]))

    def _compute_overlap(self, m1, m2):
            nrows, ncols = m1.shape
            dtype={'names':['f{}'.format(i) for i in range(ncols)],
                'formats':ncols * [m1.dtype]}

            C = np.intersect1d(m1.view(dtype), m2.view(dtype))

            # This last bit is optional if you're okay with "C" being a structured array...
            cl = C.view(m1.dtype).reshape(-1, ncols)
            if self._relative:
                denominador = np.minimum(len(m1), len(m2))
                return 100*len(cl)/denominador
            else:
                denominador = np.add(len(m1), len(m2))
                return 200*len(cl)/denominador

    def _compute_planes_overlap(self):
        self._Zoverlaps = []
        for c in range(len(self._Zlabel_l)):
            lab = self._Zlabel_l[c]
            self._Zoverlaps.append(np.zeros((len(self._Zlabel_z[c]), len(self._Zlabel_z[c]))))
            for i, z in enumerate(self._Zlabel_z[c]):
                lid_curr  = self.labels[z].index(lab)
                mask_curr = self.Masks[z][lid_curr]
                if self._fullmat:
                    zvec = self._Zlabel_z[c]
                else:
                    zvec = self._Zlabel_z[c][0:i]
                for j, zz in enumerate(zvec):
                    if zz!=z:
                        lid_other  = self.labels[zz].index(lab)
                        mask_other = self.Masks[zz][lid_other]
                        self._Zoverlaps[c][i,j]=self._compute_overlap(mask_curr, mask_other)

    def _compute_overlap_measure(self):
        self._compute_planes_overlap()
        self._Zoverlaps_conv = []
        for c, Zoverlap in enumerate(self._Zoverlaps):
            self._Zoverlaps_conv.append([])
            for z in range(Zoverlap.shape[0]):
                val = 0.0
                n   = 0
                for i in range(np.maximum(z-self._zneigh, 0), np.minimum(z+self._zneigh+1, Zoverlap.shape[0])):
                    if i!=z:
                        val+=Zoverlap[z, i]
                        n+=1
                if n == 0:
                    self._Zoverlaps_conv[-1].append(0.0)
                else:
                    self._Zoverlaps_conv[-1].append(val/n)

    def _detect_cell_barriers(self): # Need to consider changing also masks outlines etc
        self._nuclear_intensity_cell_z()
        self._compute_overlap_measure()

        self._cellbarriers = []
        for c in range(len(self._Zsignals)):
            self._cellbarriers.append([])
            intensity = np.array(self._Zsignals[c])*np.array(self._Zoverlaps_conv[c])

            # Find data valleys and their corresponding idxs
            datavalleys = np.r_[True, intensity[1:] < intensity[:-1]] & np.r_[intensity[:-1] < intensity[1:], True]
            datavalleys_idx = np.arange(0,len(datavalleys))[datavalleys]

            # Find data peaks and their corresponding idxs
            datapeaks = np.r_[True, intensity[1:] > intensity[:-1]] & np.r_[intensity[:-1] > intensity[1:], True]
            datapeaks_idx = np.arange(0,len(datapeaks))[datapeaks]

            # For each local minima, apply conditions for it to be considered a cell barrier plane.
            # Zlevel_cbs_to_pop correspongs to the z plane at which there might be a cell barrier. 
            for Zlevel_cb in datavalleys_idx:
                if Zlevel_cb==0:
                    pass
                elif Zlevel_cb==len(datavalleys)-1:
                    pass
                else:  
                    self._cellbarriers[-1].append(Zlevel_cb)

            keep_checking = True
            # remove a deltabarrier if the distance between two barriers is lower than a threshold.
            while keep_checking:                
                keep_checking=False
                Zlevel_cbs_to_pop = []
                Zlevel_cbs_to_add = []
                for i, Zlevel_cb in enumerate(self._cellbarriers[-1][0:-1]):
                    dif = self._cellbarriers[-1][i+1] - Zlevel_cb
                    if dif < 5:                  
                        if i not in Zlevel_cbs_to_pop:
                            Zlevel_cbs_to_pop.append(i)
                        if i+1 not in Zlevel_cbs_to_pop:
                            Zlevel_cbs_to_pop.append(i+1)
                        new_cb = np.argmax(intensity[Zlevel_cb:self._cellbarriers[-1][i+1]]) + Zlevel_cb
                        Zlevel_cbs_to_add.append(new_cb)
                        intensity[Zlevel_cb:self._cellbarriers[-1][i+1]+1] = np.ones(len(intensity[Zlevel_cb:self._cellbarriers[-1][i+1]+1]))*intensity[new_cb]
                        keep_checking=True
                Zlevel_cbs_to_pop.reverse()
                for i in Zlevel_cbs_to_pop:
                    self._cellbarriers[-1].pop(i)
                for new_cb in Zlevel_cbs_to_add:
                    self._cellbarriers[-1].append(new_cb)
                self._cellbarriers[-1].sort()

            Zlevel_cbs_to_pop = []
            for i, Zlevel_cb in enumerate(self._cellbarriers[-1]):
                closest_peak_right_idx  = datapeaks_idx[datapeaks_idx > Zlevel_cb].min()
                closest_peak_left_idx   = datapeaks_idx[datapeaks_idx < Zlevel_cb].max() 
                inten_peak1 = intensity[closest_peak_left_idx]
                inten_peak2 = intensity[closest_peak_right_idx]
                inten_peak  = np.minimum(inten_peak1, inten_peak2)
                inten_cb    = intensity[Zlevel_cb]
                if (inten_peak - inten_cb)/inten_peak < self._overlap_th: #0.2 threshold of relative height of the valley to the peak
                    Zlevel_cbs_to_pop.append(i)

            Zlevel_cbs_to_pop.reverse()
            for i in Zlevel_cbs_to_pop:
                self._cellbarriers[-1].pop(i)

    def _separate_concatenated_cells(self):
        self._label_per_z()
        self._detect_cell_barriers()
        zids_remove = []
        labs_remove = []
        for c, cbs in enumerate(self._cellbarriers):
            if len(cbs) != 0:
                for cb in cbs:
                    zlevel = self._Zlabel_z[c][cb]
                    label  = self._Zlabel_l[c]
                    zids_remove.append(zlevel)
                    labs_remove.append(label)
        for i, z in enumerate(zids_remove):
            lid = self.labels[z].index(labs_remove[i])
            self.labels[z].pop(lid)
            self.Outlines[z].pop(lid)
            self.Masks[z].pop(lid)
    
    def _update(self):
        self._extract_cell_centers()
        self._compute_distances_with_pre_post_z()
        self._assign_labels()
        self._label_per_z()
        self._nuclear_intensity_cell_z()
        self._compute_overlap_measure()

    def _position3d(self):
        self.labels_centers    = []
        self.centers_positions = []
        self.centers_weight    = []
        self.centers_outlines  = []
        for z in range(self.slices):
            img = self.stack[z,:,:]
            for cell, outline in enumerate(self.Outlines[z]):
                ptsin = self.Masks[z][cell]
                xs = self.centersi[z][cell]
                ys = self.centersj[z][cell]
                label = self.labels[z][cell]
                if label not in self.labels_centers:
                    self.labels_centers.append(label)
                    self.centers_positions.append([z,ys,xs])
                    self.centers_weight.append(np.sum(img[ptsin[:,1], ptsin[:,0]]))
                    self.centers_outlines.append(outline)
                else:
                    curr_weight = np.sum(img[ptsin[:,1], ptsin[:,0]])
                    idx_prev    = np.where(np.array(self.labels_centers)==label)[0][0]
                    prev_weight = self.centers_weight[idx_prev]
                    if curr_weight > prev_weight:
                        self.centers_positions[idx_prev] = [z, ys, xs]
                        self.centers_outlines[idx_prev]  = outline
                        self.centers_weight[idx_prev] = curr_weight

    def _sort_point_sequence(self, outline):
        min_dists, min_dist_idx = cKDTree(outline).query(outline,self._nearest_neighs)
        min_dists = min_dists[:,1:]
        min_dist_idx = min_dist_idx[:,1:]
        new_outline = []
        used_idxs   = []
        pidx = random.choice(range(len(outline)))
        new_outline.append(outline[pidx])
        used_idxs.append(pidx)
        while len(new_outline)<len(outline):
            a = len(used_idxs)
            for id in min_dist_idx[pidx,:]:
                if id not in used_idxs:
                    new_outline.append(outline[id])
                    used_idxs.append(id)
                    pidx=id
                    break
            if len(used_idxs)==a:
                self.printfancy("Improve your point drawing, this is a bit embarrasing") 
                self.PA.visualization()
                return
        return np.array(new_outline), used_idxs

    def _increase_point_resolution(self, outline):
        rounds = np.ceil(np.log2(self._min_outline_length/len(outline))).astype('int32')
        if rounds==0:
                newoutline_new=np.copy(outline)
        for r in range(rounds):
            if r==0:
                pre_outline=np.copy(outline)
            else:
                pre_outline=np.copy(newoutline_new)
            newoutline_new = np.copy(pre_outline)
            i=0
            while i < len(pre_outline)*2 - 2:
                newpoint = np.array([np.rint((newoutline_new[i] + newoutline_new[i+1])/2).astype('int32')])
                newoutline_new = np.insert(newoutline_new, i+1, newpoint, axis=0)
                i+=2
            newpoint = np.array([np.rint((pre_outline[-1] + pre_outline[0])/2).astype('int32')])
            newoutline_new = np.insert(newoutline_new, 0, newpoint, axis=0)

        return newoutline_new

    def update_labels(self):
        self._update()
        self._position3d()
        self.printfancy("")
        self.printfancy("## Labels updated ##")
        self.printfancy("")

    def delete_cell(self, PA):
        cells = [x[0] for x in PA.list_of_cells]
        Zs    = [x[1] for x in PA.list_of_cells]
        for i,z in enumerate(Zs):
            id_l = np.where(np.array(self.labels[z])==cells[i])[0][0]
            self.labels[z].pop(id_l)
            self.Outlines[z].pop(id_l)
            self.Masks[z].pop(id_l)
            self.centersi[z].pop(id_l)
            self.centersj[z].pop(id_l)
        self.update_labels()
        self.replot_segmented(PA.cr)
        
    # Combines cells by assigning the info for the cell with higher label number
    # to the other one. Cells must not appear in the same and they must be contiguous
    def combine_cells(self, PA):
        cells = [x[0] for x in PA.list_of_cells]
        Zs    = [x[1] for x in PA.list_of_cells]
        if len(cells)==0: return
        maxcid = cells.index(max(cells))
        id_l = np.where(np.array(self.labels[Zs[maxcid]])==max(cells))[0][0]
        self.labels[Zs[maxcid]][id_l] = min(cells)
        self.update_labels()
        self.replot_segmented(PA.cr)

    def add_cell(self, PA):
        line, = PA.ax_sel.plot([], [], linestyle="none", marker="o", color="r", markersize=2)
        self.linebuilder = LineBuilder(line)

    def complete_add_cell(self, PA):
        self.linebuilder.stopit()
        if len(self.linebuilder.xs)<3:
            return
        new_outline = np.asarray([list(a) for a in zip(np.rint(self.linebuilder.xs).astype(np.int64), np.rint(self.linebuilder.ys).astype(np.int64))])
        new_outline_sorted, _ = self._sort_point_sequence(new_outline)
        new_outline_sorted_highres = self._increase_point_resolution(new_outline_sorted)
        self.Outlines[PA.z].append(new_outline_sorted_highres)
        self.Masks[PA.z].append(self._points_within_hull(new_outline_sorted_highres))
        self.update_labels()
        self.replot_segmented(PA.cr)
        
    def _end_actions(self):
        self.printfancy("")
        print("##################     ERROR CORRECTION FINISHED     #################")
        self._returnflag = True
        return

    def printfancy(self, string, finallength=70):
        new_str = "#   "+string
        while len(new_str)<finallength-1:
            new_str+=" "
        new_str+="#"
        print(new_str)

    def printclear(self):
        LINE_UP = '\033[1A'
        LINE_CLEAR = '\x1b[2K'
        print(LINE_UP, end=LINE_CLEAR)

    def progress(self, step, total, width=46):
        percent = np.rint(step*100/total).astype('int32')
        left = width * percent // 100
        right = width - left
        
        tags = "#" * left
        spaces = " " * right
        percents = f"{percent:.0f}%"
        self.printclear()
        if percent < 10:
            print("#   Progress: [", tags, spaces, "] ", percents, "    #", sep="")
        elif 9 < percent < 100:
            print("#   Progress: [", tags, spaces, "] ", percents, "   #", sep="")
        elif percent > 99:
            print("#   Progress: [", tags, spaces, "] ", percents, "  #", sep="")
    
    def actions(self):
        print("####################       ACTION SELECTION       ####################")
        self.printfancy("")
        self.printfancy("Select one of these actions by typing the")
        self.printfancy("corresponding number:")
        self.printfancy("")
        self.printfancy("1 - Plot embryo")
        self.printfancy("2 - Update labels")
        self.printfancy("3 - Undo all actions and redo segmentation")
        self.printfancy("4 - END action selection")
        self.printfancy("")
        act=input("#   SELECTION: ")
        self.printclear()    
        self.printfancy("SELECTION: "+act)
        self._returnflag = False
        try:
            chosen_action = int(act)
            if chosen_action in [1,2,3,4]:
                self.printfancy("")
                self._actions[chosen_action-1]()
                if self._returnflag==True:
                    return
                self.printfancy("")
                self.actions()
                return 
            else:
                self.printfancy("")
                self.printfancy("ERROR: Please select one of the given options")
                self.printfancy("")
                self.actions()
                return
        except ValueError:
            self.printfancy("")
            self.printfancy("ERROR: Please select one of the given options")
            self.printfancy("")
            self.actions()
            return
    
    def compute_Masks_to_plot(self):
        self._Masks_to_plot = np.zeros_like(self.stack, dtype=np.int32)
        self._Masks_to_plot_alphas = np.zeros_like(self.stack, dtype=np.int32)
        for z in range(0,self.slices):
            for cell in range(0,len(self.Masks[z])):
                lab = self.labels[z][cell]
                for idpair in self.Masks[z][cell]:
                    id1 = idpair[0]
                    id2 = idpair[1]
                    self._Masks_to_plot[z][id2][id1] = self._labels_color_id[lab]
                    self._Masks_to_plot_alphas[z][id2][id1] = 1

    def _assign_color_to_label(self):
        coloriter = itertools.cycle([i for i in range(len(self._masks_colors))])
        self._labels_color_id = [next(coloriter) for i in range(1000)]

    def plot_segmented(self):
        self.printfancy("")
        self.printfancy("Proceeding to plot cell segmentation")
        self.printfancy("- To plot a specific PLANE type the number")            
        self.printfancy("- Press ENTER to plot layout")
        self.printfancy("- Press B to go back to action selection")
        self.printfancy("")
        self.plot_only_z=input("#   SELECTION: ")
        self.printclear()    
        self.printfancy("SELECTION: "+self.plot_only_z)
        if self.plot_only_z == 'B':
            self.actions()
            return
        try:
            self.plot_only_z = int(self.plot_only_z)
        except:
            self.plot_only_z=None

        pltmasks = input("#   PLOT MASKS? (y/n), ENTER for default: ")
        self.printclear()    
        self.printfancy("PLOT MASKS? (y/n), ENTER for default: "+pltmasks)
        self.pltmasks_bool = self.plot_masks
        if pltmasks == 'y':
            self.pltmasks_bool = True
        elif pltmasks == 'n':
            self.pltmasks_bool = False

        if self.plot_only_z!=None:
            if isinstance(self.plot_only_z, int) and self.plot_only_z in range(self.slices):
                self.printfancy("Plotting plane z = %d" %self.plot_only_z)

                img = self.stack[self.plot_only_z,:,:]

                # plot
                fig, ax = plt.subplots()
                self.plot_axis(ax, img, self.plot_only_z)
                self.PA = PlotActionCS(fig, ax, self.plot_only_z, self, self.plot_only_z)
                plt.show()

                self.printfancy("")
                self.printfancy("## Plotting completed ##")
                self.printfancy("")
                return
            else:
                self.printfancy("")
                self.printfancy("Please give a proper plane number. Going back to action selection")
                self.printfancy("")
                self.actions()
                return
        
        self.printfancy("")
        self.printfancy("## Plotting layout ##")
        
        # tuple with dimension 2 containing vectors of size=groupsize
        # each element is the correspondent index for a given z plane
        counter = plotRound(layout=self.plot_layout,totalsize=self.slices, overlap=self.plot_overlap, round=0)
        fig, ax = plt.subplots(counter.layout[0],counter.layout[1], figsize=(10,10))
        zs = np.empty_like(ax)
        self.PA = PlotActionCS(fig, ax, zs, self, 0)
        if len(ax.shape)==1:
            zidxs = range(counter.groupsize)
        else:
            zidxs  = np.unravel_index(range(counter.groupsize), counter.layout)

        # Plot all our Zs in the corresponding round
        for z, id, round in counter:
            
            # select current z plane
            if len(ax.shape)==1:
                idx = zidxs[id]
                if z == None:
                    ax[idx].axis(False)
                else:
                    img = self.stack[z,:,:]

                    # select corresponding idxs on the plot 
                    self.PA.zs[idx] = z

                    # plot
                    self.plot_axis(ax[idx], img, z)
                
            else:
                if z == None:
                    ax[idx1,idx2].axis(False)
                else:      
                    # select corresponding idxs on the plot 
                    img = self.stack[z,:,:]
                    idx1 = zidxs[0][id]
                    idx2 = zidxs[1][id]
                    self.PA.zs[idx1, idx2] = z

                    # plot
                    self.plot_axis(ax[idx1, idx2], img, z)
                    
        plt.show()
        self.printfancy("")
        self.printfancy("## Plotting completed ##")
        self.printfancy("")

    def replot_segmented(self, round):
        if isinstance(self.PA.ax, np.ndarray):
            counter = plotRound(layout=self.plot_layout,totalsize=self.slices, overlap=self.plot_overlap, round=round)

            if len(self.PA.ax.shape)==1:
                zidxs = range(counter.groupsize)
            else:
                zidxs  = np.unravel_index(range(counter.groupsize), counter.layout)

            # Plot all our Zs in the corresponding round
            
            for z, id, r in counter:
                if len(self.PA.ax.shape)==1:
                    idx = zidxs[id]
                    if z == None:
                        self.PA.ax[idx].axis(False)
                    else:
                        self.PA.zs[idx] = z
                        img = self.stack[z,:,:]

                        self.PA.ax[idx].cla()
                        self.plot_axis(self.PA.ax[idx], img, z)
                else:
                    idx1 = zidxs[0][id]
                    idx2 = zidxs[1][id]
                    if z == None:
                        self.ax[idx1,idx2].axis(False)
                    else:      
                        # select corresponding idxs on the plot 
                        self.PA.zs[idx1, idx2] = z
                        img = self.stack[z,:,:]

                        # plot
                        self.PA.ax[idx1, idx2].cla()
                        self.plot_axis(self.PA.ax[idx1, idx2], img, z)
        else:
            img  = self.stack[round,:,:]
            self.PA.zs = round
            self.PA.z  = round
            self.PA.ax.cla()
            self.plot_axis(self.PA.ax, img, round)

    def plot_axis(self, _ax, img, z):
        _ = _ax.imshow(img)
        _ = _ax.set_title("z = %d" %z)
        for cell, outline in enumerate(self.Outlines[z]):
            xs = self.centersi[z][cell]
            ys = self.centersj[z][cell]
            label = self.labels[z][cell]
            _ = _ax.scatter(outline[:,0], outline[:,1], c=[self._masks_colors[self._labels_color_id[label]]], s=0.5, cmap=self._masks_cmap_name)               
            _ = _ax.annotate(str(label), xy=(ys, xs), c="w")
            _ = _ax.scatter([ys], [xs], s=0.5, c="white")
            _ = _ax.axis(False)

        if self.pltmasks_bool:
            self.compute_Masks_to_plot()
            _ = _ax.imshow(self._masks_cmap(self._Masks_to_plot[z], alpha=self._Masks_to_plot_alphas[z], bytes=True), cmap=self._masks_cmap_name)
        for lab in range(len(self.labels_centers)):
            zz = self.centers_positions[lab][0]
            ys = self.centers_positions[lab][1]
            xs = self.centers_positions[lab][2]
            if zz==z:
                _ = _ax.scatter([ys], [xs], s=3.0, c="k")

class PlotActionCS:
    def __init__(self, fig, ax, zs, CS, current_round):
        self.fig=fig
        self.ax=ax
        self.CS=CS
        self.get_size()
        actionsbox = "Possible actions      - ESC : visualization  \n- q : quit plot           - a     : add cells        \n- d : delete cell       - c      : combine cells"
        self.actionlist = self.fig.text(0.98, 0.98, actionsbox, fontsize=self.figheight/90, ha='right', va='top')
        self.title = self.fig.suptitle("", x=0.01, ha='left', fontsize=self.figheight/70)
        self.instructions = self.fig.text(0.2, 0.98, "instructions", fontsize=self.figheight/70, ha='left', va='top')
        self.selected_cells = self.fig.text(0.98, 0.89, "Cell\nSelection", fontsize=self.figheight/90, ha='right', va='top')
        self.list_of_cells = []
        self.act = fig.canvas.mpl_connect('key_press_event', self)
        self.scl = fig.canvas.mpl_connect('scroll_event', self.onscroll)
        if isinstance(zs, np.ndarray):
            groupsize  = self.CS.plot_layout[0] * self.CS.plot_layout[1]
            self.max_round =  math.ceil((self.CS.slices)/(groupsize-self.CS.plot_overlap))
        else:
            self.max_round = self.CS.slices
        self.cr = current_round
        self.visualization()
        self.update()
        self.current_state=None
        self.current_subplot = None
        self.zs = zs
        if isinstance(self.zs, int):
            self.z=self.zs
        else:
            self.z = None
    
    def onscroll(self, event):
        if self.current_state==None:
            self.current_state="SCL"
            if event.button == 'up':
                self.cr = self.cr + 1
            elif event.button == 'down':
                self.cr = self.cr - 1
            self.cr = max(self.cr, 0)
            self.cr = min(self.cr, self.max_round)
            self.CS.replot_segmented(self.cr)
            self.update()
        else:
            self.CS.printfancy("Currently on another action")
            return
        self.current_state=None

    def __call__(self, event):
        if self.current_state==None:
            if event.key == 'a':
                self.CS.printfancy("")
                self.CS.printfancy("# Entering ADD mode. Press ENTER to exit #")
                self.current_state="add"
                self.visualization()
                self.add_cells()
            elif event.key == 'd':
                self.CS.printfancy("")
                self.CS.printfancy("# Entering DELETE mode. Press ENTER to exit #")
                self.current_state="del"
                self.delete_cells()
            elif event.key == 'c':
                self.CS.printfancy("")
                self.CS.printfancy("# Entering COMBINE mode. Press ENTER to exit #")
                self.current_state="com"
                self.combine_cells()
            elif event.key == 'escape':
                self.CS.printfancy("")
                self.CS.printfancy("# Entering VISUALIZATION mode #")
                self.visualization()
            self.update()
        else:
            if event.key=='enter':
                if self.current_state=="add":
                    if self.current_subplot==None:
                        pass
                    elif self.current_subplot=='single':
                        self.CS.complete_add_cell(self)
                    else:
                        self.ax_sel.patches.remove(self.patch)
                        self.CS.complete_add_cell(self)
                elif self.current_state=="del":
                    self.CP.stopit()
                    self.CS.delete_cell(self)
                    self.list_of_cells = []
                elif self.current_state=="com":
                    self.CP.stopit()
                    self.CS.combine_cells(self)
                    self.list_of_cells = []
                self.visualization()
                self.update()
                self.current_subplot=None
                self.current_state=None
                self.ax_sel=None
                if isinstance(self.zs, int):
                    self.z=self.zs
                else:
                    self.z = None
            else:
                # We have to wait for the current action to finish
                pass

    def update(self): 
        self.get_size()
        if self.figheight < self.figwidth:
            width_or_height = self.figheight
        else:
            width_or_height = self.figwidth
        self.actionlist.set(fontsize=width_or_height/90)
        self.selected_cells.set(fontsize=width_or_height/90)
        cells_to_plot = self.sort_list_of_cells()
        cells_string = ["cell="+str(x[0])+" z="+str(x[1]) for x in cells_to_plot]
        s = "\n".join(cells_string)
        self.selected_cells.set(text="Cells\nSelected\n\n"+s)
        self.instructions.set(fontsize=width_or_height/70)
        self.title.set(fontsize=width_or_height/70)
        plt.subplots_adjust(top=0.9,right=0.8)
        self.fig.canvas.draw()

    def sort_list_of_cells(self):
        if len(self.list_of_cells)==0:
            return self.list_of_cells
        else:
            cells = [x[0] for x in self.list_of_cells]
            Zs    = [x[1] for x in self.list_of_cells]

            cidxs  = np.argsort(cells)
            cells = np.array(cells)[cidxs]
            Zs    = np.array(Zs)[cidxs]

            ucells = np.unique(cells)
            final_cells = []
            for c in ucells:
                ids = np.where(cells == c)
                _cells = cells[ids]
                _Zs    = Zs[ids]
                zidxs = np.argsort(_Zs)
                for id in zidxs:
                    final_cells.append([_cells[id], _Zs[id]])

            return final_cells

    def add_cells(self):
        self.title.set(text="ADD CELL\nMODE", ha='left', x=0.01)
        if isinstance(self.ax, np.ndarray):
            if len(self.ax.shape)==1:
                if self.current_subplot == None:
                    self.instructions.set(text="DOUBLE LEFT-CLICK TO SELECT Z-PLANE", ha='left', x=0.2)
                    self.fig.patch.set_facecolor((0.0,1.0,0.0,0.2))
                    SP = SubplotPicker_add(self, self.add_cells, self.CS.add_cell)
                else:
                    i = self.current_subplot
                    self.ax_sel = self.ax[i]
                    bbox = self.ax_sel.get_window_extent()
                    self.patch =mtp.patches.Rectangle((bbox.x0 - bbox.width*0.1, bbox.y0-bbox.height*0.1),
                                        bbox.width*1.2, bbox.height*1.2,
                                        fill=True, color=(0.0,1.0,0.0), alpha=0.4, zorder=-1,
                                        transform=None, figure=self.fig)
                    self.fig.patches.extend([self.patch])
                    self.instructions.set(text="Right click to add points\nPress ENTER when finished", ha='left', x=0.2)
                    self.update()
                    self.ax_sel.add_patch(self.patch)
            else:
                if self.current_subplot == None:
                    self.instructions.set(text="DOUBLE LEFT-CLICK TO SELECT Z-PLANE", ha='left', x=0.2)
                    self.fig.patch.set_facecolor((0.0,1.0,0.0,0.2))
                    SP = SubplotPicker_add(self, self.add_cells, self.CS.add_cell)
                else:
                    i = self.current_subplot[0]
                    j = self.current_subplot[1]
                    self.ax_sel = self.ax[i,j]
                    m, n = self.ax.shape
                    bbox00 = self.ax[0, 0].get_window_extent()
                    bbox01 = self.ax[0, 1].get_window_extent()
                    bbox10 = self.ax[1, 0].get_window_extent()
                    pad_h = 0 if n == 1 else bbox01.x0 - bbox00.x0 - bbox00.width
                    pad_v = 0 if m == 1 else bbox00.y0 - bbox10.y0 - bbox10.height
                    bbox = self.ax_sel.get_window_extent()
                    self.patch =mtp.patches.Rectangle((bbox.x0 - pad_h / 2, bbox.y0 - pad_v / 2),
                                        bbox.width + pad_h, bbox.height + pad_v,
                                        fill=True, color=(0.0,1.0,0.0), alpha=0.4, zorder=-1,
                                        transform=None, figure=self.fig)
                    self.fig.patches.extend([self.patch])
                    self.instructions.set(text="Right click to add points\nPress ENTER when finished", ha='left', x=0.2)
                    self.update()
                    self.ax_sel.add_patch(self.patch)
        else:
            self.current_subplot='single'
            self.ax_sel = self.ax
            self.fig.patch.set_facecolor((0.0,1.0,0.0,0.3))
            self.CS.add_cell(self)
    
    def delete_cells(self):
        self.title.set(text="DELETE CELL\nMODE", ha='left', x=0.01)
        self.instructions.set(text="Right-click to delete cell on a plane\ndouble right-click to delete on all planes", ha='left', x=0.2)
        self.fig.patch.set_facecolor((1.0,0.0,0.0,0.3))
        self.CP = CellPicker_del(self)
    
    def combine_cells(self):
        self.title.set(text="COMBINE CELLS\nMODE", ha='left', x=0.01)
        self.instructions.set(text="\nRigth-click to select cells to be combined", ha='left', x=0.2)
        self.fig.patch.set_facecolor((0.0,0.0,1.0,0.3))
        self.CP = CellPicker_com(self)

    def visualization(self):
        self.title.set(text="VISUALIZATION\nMODE", ha='left', x=0.01)
        self.instructions.set(text="Chose one of the actions to change mode", ha='left', x=0.2)
        self.fig.patch.set_facecolor((1.0,1.0,1.0,1.0))

    def get_size(self):
        bboxfig = self.fig.get_window_extent().transformed(self.fig.dpi_scale_trans.inverted())
        widthfig, heightfig = bboxfig.width*self.fig.dpi, bboxfig.height*self.fig.dpi
        self.figwidth  = widthfig
        self.figheight = heightfig

class SubplotPicker_add():
    def __init__(self, PA, f1=None,f2=None):
        self.PA  = PA
        self.cid = self.PA.fig.canvas.mpl_connect('button_press_event', self)
        if f1==None:
            self.f1 = PA.passfunc
        else:
            self.f1 = f1
        if f2==None:
            self.f2 = PA.passfunc
        else:
            self.f2 = f2
        self.axshape = self.PA.ax.shape
        self.canvas  = self.PA.fig.canvas

    def __call__(self, event):
        if event.dblclick == True:
            if event.button==1:
                if len(self.axshape)==1:
                    for i in range(self.axshape[0]):
                        if event.inaxes==self.PA.ax[i]:
                            self.PA.current_subplot = i
                            self.PA.z = self.PA.zs[i]
                            self.canvas.mpl_disconnect(self.cid)
                            self.f1()
                            self.f2(self.PA)
                else:
                    for i in range(self.axshape[0]):
                        for j in range(self.axshape[1]):
                                if event.inaxes==self.PA.ax[i,j]:
                                    self.PA.current_subplot = [i,j]
                                    self.PA.z = self.PA.zs[i,j]
                                    self.canvas.mpl_disconnect(self.cid)
                                    self.f1()
                                    self.f2(self.PA)

class CellPicker_del():
    def __init__(self, PA):
        self.PA  = PA
        self.cid = self.PA.fig.canvas.mpl_connect('button_press_event', self)
        self.canvas  = self.PA.fig.canvas
    def __call__(self, event):
        if event.button==3:
            if isinstance(self.PA.ax, np.ndarray):
                axshape = self.PA.ax.shape
                # Select ax 
                if len(axshape)==1:
                    for i in range(axshape[0]):
                        if event.inaxes==self.PA.ax[i]:
                            self.PA.current_subplot = [i]
                            self.PA.ax_sel = self.PA.ax[i]
                            self.PA.z = self.PA.zs[i]
                else:
                    for i in range(axshape[0]):
                            for j in range(axshape[1]):
                                    if event.inaxes==self.PA.ax[i,j]:
                                        self.PA.current_subplot = [i,j]
                                        self.PA.ax_sel = self.PA.ax[i,j]
                                        self.PA.z = self.PA.zs[i,j]
            else:
                self.PA.ax_sel = self.PA.ax
                self.PA.z = self.PA.zs

            if event.inaxes!=self.PA.ax_sel:
                pass
            else:
                x = np.rint(event.xdata).astype(np.int64)
                y = np.rint(event.ydata).astype(np.int64)
                picked_point = np.array([x, y])
                for i ,mask in enumerate(self.PA.CS.Masks[self.PA.z]):
                    for point in mask:
                        if (picked_point==point).all():
                            z   = self.PA.z
                            lab = self.PA.CS.labels[z][i]
                            cell = [lab, z]
                            if cell not in self.PA.list_of_cells:
                                self.PA.list_of_cells.append(cell)
                            else:
                                self.PA.list_of_cells.remove(cell)
                            if event.dblclick==True:
                                idx_lab = np.where(np.array(self.PA.CS._Zlabel_l)==lab)[0][0]
                                zs = self.PA.CS._Zlabel_z[idx_lab]
                                add_all=True
                                idxtopop=[]
                                for jj, _cell in enumerate(self.PA.list_of_cells):
                                    _lab = _cell[0]
                                    _z   = _cell[1]
                                    if _lab == lab:
                                        if _z in zs:
                                            add_all=False
                                            idxtopop.append(jj)
                                idxtopop.sort(reverse=True)
                                for jj in idxtopop:
                                    self.PA.list_of_cells.pop(jj)
                                if add_all:
                                    for zz in zs:
                                        self.PA.list_of_cells.append([lab, zz])
                            self.PA.update()
            # Select cell and store it   

    def stopit(self):
        self.canvas.mpl_disconnect(self.cid)

class CellPicker_com():
    def __init__(self, PA):
        self.PA  = PA
        self.cid = self.PA.fig.canvas.mpl_connect('button_press_event', self)
        self.canvas  = self.PA.fig.canvas
    def __call__(self, event):
        if event.button==3:
            if isinstance(self.PA.ax, np.ndarray):
                axshape = self.PA.ax.shape
                # Select ax 
                if len(axshape)==1:
                    for i in range(axshape[0]):
                        if event.inaxes==self.PA.ax[i]:
                            self.PA.current_subplot = [i]
                            self.PA.ax_sel = self.PA.ax[i]
                            self.PA.z = self.PA.zs[i]
                else:
                    for i in range(axshape[0]):
                            for j in range(axshape[1]):
                                    if event.inaxes==self.PA.ax[i,j]:
                                        self.PA.current_subplot = [i,j]
                                        self.PA.ax_sel = self.PA.ax[i,j]
                                        self.PA.z = self.PA.zs[i,j]
            else:
                self.PA.ax_sel = self.PA.ax

            if event.inaxes!=self.PA.ax_sel:
                pass
            else:
                x = np.rint(event.xdata).astype(np.int64)
                y = np.rint(event.ydata).astype(np.int64)
                picked_point = np.array([x, y])
                for i ,mask in enumerate(self.PA.CS.Masks[self.PA.z]):
                    for point in mask:
                        if (picked_point==point).all():
                            z   = self.PA.z
                            lab = self.PA.CS.labels[z][i]
                            cell = [lab, z]
                            if cell not in self.PA.list_of_cells:
                                if len(self.PA.list_of_cells)==2:
                                    self.PA.CS.printfancy("Can only combine two cells at one")
                                self.PA.list_of_cells.append(cell)
                            else:
                                self.PA.list_of_cells.remove(cell)
                            self.PA.update()
            # Select cell and store it   

    def stopit(self):
        self.canvas.mpl_disconnect(self.cid)
                 
class LineBuilder:
    def __init__(self, line):
        self.line = line
        self.xs = list(line.get_xdata())
        self.ys = list(line.get_ydata())
        self.cid = line.figure.canvas.mpl_connect('button_press_event', self)

    def __call__(self, event):
        if event.inaxes!=self.line.axes: 
            return
        if event.button==3:
            self.xs.append(event.xdata)
            self.ys.append(event.ydata)
            self.line.set_data(self.xs, self.ys)
            self.line.figure.canvas.draw()
        else:
            return
    def stopit(self):
        self.line.figure.canvas.mpl_disconnect(self.cid)
        self.line.remove()
    
class plotCounter:
    def __init__(self, layout, totalsize, overlap ):
        self.current = -1
        self.currentonround = -1
        self.totalsize  = totalsize
        self.overlap    = overlap
        self.layout     = layout
        self.groupsize  = self.layout[0] * self.layout[1]
        self.rounds     = math.ceil((self.totalsize)/(self.groupsize-self.overlap))
        self.currentround = 0
        self.totalspots = self.groupsize*self.rounds
        self.totalplots = (self.rounds-1)*self.overlap + self.totalsize
        self.emptyspots = self.totalspots - self.totalplots
        if self.rounds == 1:
            self.groupsize=self.totalspots
        if self.emptyspots+self.overlap == self.groupsize:
            self.rounds -= 1
            self.totalspots = self.groupsize*self.rounds
            self.totalplots = (self.rounds-1)*self.overlap + self.totalsize
            self.emptyspots = self.totalspots - self.totalplots

    def __iter__(self):
        return self

    def __next__(self):
        self.current += 1
        self.currentonround += 1
        if self.currentonround==self.groupsize:
            self.currentround+=1
            self.current -= self.overlap
            self.currentonround=0
        if self.current < self.totalsize and self.currentround < self.rounds:
            return self.current, self.currentonround, self.currentround
        raise StopIteration

class plotRound:
    def __init__(self, layout, totalsize, overlap, round):
        self.totalsize  = totalsize
        self.overlap    = overlap
        self.layout     = layout
        self.groupsize  = self.layout[0] * self.layout[1]
        
        self.current = (self.groupsize*round -1)-(self.overlap*round)
        self.currentonround = -1

        self.rounds     = math.ceil((self.totalsize)/(self.groupsize-self.overlap))
        self.currentround = round
        if self.rounds == 1:
            self.groupsize=self.totalspots
    
    def __iter__(self):
        return self

    def __next__(self):
        self.current += 1
        self.currentonround += 1
        if self.currentonround==self.groupsize:
            raise StopIteration
        if self.current < self.totalsize and self.currentround < self.rounds:
            return self.current, self.currentonround, self.currentround
        else:
            return None, self.currentonround, self.currentround

class CellTracking(object):
    def __init__(self, stacks, model, trainedmodel=None, channels=[0,0], flow_th_cellpose=0.4, distance_th_z=3.0, xyresolution=0.2767553, relative_overlap=False, use_full_matrix_to_compute_overlap=True, z_neighborhood=2, overlap_gradient_th=0.3, plot_layout=(2,2), plot_overlap=1, plot_layout_time=(2,3), plot_overlap_time=1,plot_masks=True, masks_cmap='tab10', min_outline_length=200, neighbors_for_sequence_sorting=7):
        self.stacks            = stacks
        self._model            = model
        self._trainedmodel     = trainedmodel
        self._channels         = channels
        self._flow_th_cellpose = flow_th_cellpose
        self._distance_th_z    = distance_th_z
        self._xyresolution     = xyresolution
        self.times             = np.shape(stacks)[0]
        self.slices            = np.shape(stacks)[1]
        self.stack_dims        = np.shape(stacks)[-2:]
        self._relative         = relative_overlap
        self._fullmat          = use_full_matrix_to_compute_overlap
        self._zneigh           = z_neighborhood
        self._overlap_th       = overlap_gradient_th # is used to separed cells that could be consecutive on z
        self.plot_layout       = plot_layout
        self.plot_overlap      = plot_overlap
        self.plot_masks        = plot_masks
        self.plot_layout_time  = plot_layout_time
        self.plot_overlap_time = plot_overlap_time
        self._max_label        = 0
        self._masks_cmap_name  = masks_cmap
        self._masks_cmap       = cm.get_cmap(self._masks_cmap_name)
        self._masks_colors     = self._masks_cmap.colors
        self._min_outline_length = min_outline_length
        self._nearest_neighs     = neighbors_for_sequence_sorting
        self.cells_to_combine  = []

    def __call__(self):
        self.cell_segmentation()
        self.cell_tracking()
        self.copyCT = deepcopy(self)

    def undo_corrections(self):
        self.TLabels   = deepcopy(self.copyCT.TLabels)
        self.TCenters  = deepcopy(self.copyCT.TCenters)
        self.TOutlines = deepcopy(self.copyCT.TOutlines)
        self.CSt       = deepcopy(self.copyCT.CSt)
        self.FinalLabels  = deepcopy(self.copyCT.FinalLabels)
        self.FinalCenters = deepcopy(self.copyCT.FinalCenters)
        self.FinalOulines = deepcopy(self.copyCT.FinalOulines)
        self.label_correspondance = deepcopy(self.copyCT.label_correspondance)
        
    def cell_segmentation(self):
        self.TLabels   = []
        self.TCenters  = []
        self.TOutlines = []
        self.CSt       = []
        for t in range(self.times):
            print("\n\n ######   CURRENT TIME = %d   ######\n" %t)
            imgs = self.stacks[t,:,:,:]
            CS = CellSegmentation( imgs, self._model, trainedmodel=self._trainedmodel
                                , channels=self._channels
                                , flow_th_cellpose=self._flow_th_cellpose
                                , distance_th_z=self._distance_th_z
                                , xyresolution=self._xyresolution
                                , relative_overlap=self._relative
                                , use_full_matrix_to_compute_overlap=self._fullmat
                                , z_neighborhood=self._zneigh
                                , overlap_gradient_th=self._overlap_th
                                , plot_layout=self.plot_layout
                                , plot_overlap=self.plot_overlap
                                , plot_masks=self.plot_masks
                                , masks_cmap=self._masks_cmap_name
                                , min_outline_length=self._min_outline_length
                                , neighbors_for_sequence_sorting=self._nearest_neighs)

            print("Segmenting stack for current time...")          
            CS()
            print("Segmentation and corrections completed. Proceeding to next time")
            self.CSt.append(CS)
    
    def extract_labels(self):
        self.TLabels   = []
        self.TCenters  = []
        self.TOutlines = []
        self.label_correspondance = []
        for t in range(self.times):
            self.TLabels.append(self.CSt[t].labels_centers)
            self.TCenters.append(self.CSt[t].centers_positions)
            self.TOutlines.append(self.CSt[t].centers_outlines)
            self.label_correspondance.append([])

    def cell_tracking(self):
        self.extract_labels()
        TLabels  = self.TLabels
        TCenters = self.TCenters
        TOutlines = self.TOutlines
        FinalLabels   = []
        FinalCenters  = []
        FinalOutlines = []
        for t in range(np.shape(self.stacks)[0]):
            print("T = ", t)
            if t==0:
                FinalLabels.append(TLabels[0])
                FinalCenters.append(TCenters[0])
                FinalOutlines.append(TOutlines[0])
                labmax = np.max(FinalLabels[0])
                for lab in TLabels[0]:
                    self.label_correspondance[0].append([lab, lab])
            else:
                FinalLabels.append([])
                FinalCenters.append([])
                FinalOutlines.append([])

                Dists = np.ones((len(FinalLabels[t-1]), len(TLabels[t])))
                for i in range(len(FinalLabels[t-1])):
                    poscell1 = np.array(FinalCenters[t-1][i][1:])*np.array([0.2767553, 0.2767553])
                    for j in range(len(TLabels[t])): 
                        poscell2 = np.array(TCenters[t][j][1:])*np.array([0.2767553, 0.2767553])
                        Dists[i,j] = np.linalg.norm(poscell1-poscell2)
                        if np.abs(FinalCenters[t-1][i][0] - TCenters[t][j][0])>2:
                            Dists[i,j] = 100.0

                #iDists = 1/Dists
                #col_sums = iDists.sum(axis=0)
                #piDists1 = iDists/col_sums[np.newaxis, :]
                #row_sums = iDists.sum(axis=1)
                #piDists2 = iDists/row_sums[:, np.newaxis]
                #pMat = piDists1*piDists2

                a = np.argmin(Dists, axis=0) # max prob for each future cell to be a past cell
                b = np.argmin(Dists, axis=1) # max prob for each past cell to be a future one
                correspondance = []
                notcorrespondenta = []
                notcorrespondentb = []
                for i,j in enumerate(b):
                    if i==a[j]:
                        if Dists[i,j] < 7.5:
                            correspondance.append([i,j]) #[past, future]
                            self.label_correspondance[t].append([TLabels[t][j], FinalLabels[t-1][i]])
                            FinalLabels[t].append(FinalLabels[t-1][i])
                            FinalCenters[t].append(TCenters[t][j])
                            FinalOutlines[t].append(TOutlines[t][j])                            
                    else:
                        notcorrespondenta.append(i)
                labmax = np.maximum(np.max(FinalLabels[t-1]), labmax)
                for j in range(len(a)):
                    if j not in np.array(correspondance)[:,1]:
                        self.label_correspondance[t].append([TLabels[t][j], labmax+1])
                        FinalLabels[t].append(labmax+1)
                        labmax+=1
                        FinalCenters[t].append(TCenters[t][j])
                        FinalOutlines[t].append(TOutlines[t][j])
                        notcorrespondentb.append(j)
                
        self.FinalLabels  = FinalLabels
        self.FinalCenters = FinalCenters
        self.FinalOulines = FinalOutlines
        None
    
    def update_labels(self):
        pass
    
    def delete_cell(self, PA):
        cells = [x[0] for x in PA.list_of_cells]
        Zs    = [x[1] for x in PA.list_of_cells]
        for i,z in enumerate(Zs):
            id_l = np.where(np.array(PA.CS.labels[z])==cells[i])[0][0]
            PA.CS.labels[z].pop(id_l)
            PA.CS.Outlines[z].pop(id_l)
            PA.CS.Masks[z].pop(id_l)
            PA.CS.centersi[z].pop(id_l)
            PA.CS.centersj[z].pop(id_l)
        
        PA.CS.update_labels()
        self.cell_tracking()

    def combine_cells(self, PA):
        if len(self.cells_to_combine)==0:
            return
        cells = [x[0] for x in self.cells_to_combine]
        Ts    = [x[1] for x in self.cells_to_combine]
        IDsco = [x[2] for x in self.cells_to_combine]

        maxlabidx = np.argmax(cells)
        minlabidx = np.argmin(cells)
        maxlab = max(cells)
        minlab = min(cells)

        idd = np.where(np.array(self.FinalLabels[Ts[maxlabidx]])==maxlab)[0][0]         
        self.FinalLabels[Ts[maxlabidx]][idd] = minlab
        self.label_correspondance[Ts[maxlabidx]][IDsco[maxlabidx]][1] = minlab

    def plot_tracking(self):
        counter = plotRound(layout=self.plot_layout,totalsize=self.slices, overlap=self.plot_overlap, round=0)
        fig, ax = plt.subplots(counter.layout[0],counter.layout[1], figsize=(10,10))
        self.zs = np.zeros_like(ax)
        self.PACT = PlotActionCT(fig, ax, self)
        zidxs  = np.unravel_index(range(counter.groupsize), counter.layout)
        t=0
        IMGS = self.stacks
        FinalCenters = self.FinalCenters
        FinalLabels  = self.FinalLabels
        imgs   = IMGS[t,:,:,:]
        # Plot all our Zs in the corresponding round
        for z, id, round in counter:
            # select current z plane
            idx1 = zidxs[0][id]
            idx2 = zidxs[1][id]
            ax[idx1,idx2].axis(False)
            if z == None:
                pass
            else:      
                img = imgs[z,:,:]
                self.zs[idx1, idx2] = z
                self.plot_axis(self.CSt[t], ax[idx1, idx2], img, z, t)
                for lab in range(len(FinalLabels[t])):
                    z = FinalCenters[t][lab][0]
                    ys = FinalCenters[t][lab][1]
                    xs = FinalCenters[t][lab][2]
                    #_ = ax[idx1, idx2].scatter(FinalOutlines[t][lab][:,0], FinalOutlines[t][lab][:,1], s=0.5)
                    _ = ax[idx1, idx2].scatter([ys], [xs], s=1.0, c="white")
                    _ = ax[idx1, idx2].annotate(str(FinalLabels[t][lab]), xy=(ys, xs), c="white")
                    _ = ax[idx1, idx2].set_xticks([])
                    _ = ax[idx1, idx2].set_yticks([])
        plt.subplots_adjust(bottom=0.075)
        # Make a horizontal slider to control the frequency.
        axslide = fig.add_axes([0.12, 0.01, 0.8, 0.03])
        self.time_slider = Slider(
            ax=axslide,
            label='time',
            valmin=0,
            valmax=self.times,
            valinit=0,
            valstep=1
        )
        self.time_slider.on_changed(self.update_slider)
        plt.show()

    # The function to be called anytime a slider's value changes
    def update_slider(self, t):
        self.PACT.t=t
        self.PACT.CS = self.CSt[t]
        self.PACT.CT.replot_tracking()
        self.PACT.update()

    def replot_tracking(self, round):
        t = self.PACT.t
        counter = plotRound(layout=self.plot_layout,totalsize=self.slices, overlap=self.plot_overlap, round=round)
        zidxs  = np.unravel_index(range(counter.groupsize), counter.layout)
        IMGS = self.stacks
        FinalCenters = self.FinalCenters
        FinalLabels  = self.FinalLabels
        imgs   = IMGS[t,:,:,:]
        # Plot all our Zs in the corresponding round
        for z, id, r in counter:
            # select current z plane
            idx1 = zidxs[0][id]
            idx2 = zidxs[1][id]
            self.PACT.ax[idx1,idx2].cla()
            self.PACT.ax[idx1,idx2].axis(False)
            if z == None:
                pass
            else:      
                img = imgs[z,:,:]
                self.zs[idx1, idx2] = z
                self.plot_axis(self.CSt[t], self.ax[idx1, idx2], img, z, t)
                for lab in range(len(FinalLabels[t])):
                    z = FinalCenters[t][lab][0]
                    ys = FinalCenters[t][lab][1]
                    xs = FinalCenters[t][lab][2]
                    idx1 = zidxs[0][z]
                    idx2 = zidxs[1][z]
                    #_ = ax[idx1, idx2].scatter(FinalOutlines[t][lab][:,0], FinalOutlines[t][lab][:,1], s=0.5)
                    _ = self.PACT.ax[idx1, idx2].scatter([ys], [xs], s=1.0, c="white")
                    _ = self.PACT.ax[idx1, idx2].annotate(str(FinalLabels[t][lab]), xy=(ys, xs), c="white")
                    _ = self.PACT.ax[idx1, idx2].set_xticks([])
                    _ = self.PACT.ax[idx1, idx2].set_yticks([])
        plt.subplots_adjust(bottom=0.075)
        # Make a horizontal slider to control the frequency.

    def plot_axis(self, CS, _ax, img, z, t):
        _ = _ax.imshow(img)
        _ = _ax.set_title("z = %d" %z)
        _ = _ax.axis(False)

        for cell, outline in enumerate(CS.Outlines[z]):
            xs = CS.centersi[z][cell]
            ys = CS.centersj[z][cell]
            label = CS.labels[z][cell]
            idd = np.where(np.array(self.label_correspondance[t])[:,0]==label)[0][0]
            Tlab = self.label_correspondance[t][idd][1]
            _ = _ax.scatter(outline[:,0], outline[:,1], c=[CS._masks_colors[CS._labels_color_id[Tlab]]], s=0.5, cmap=CS._masks_cmap_name)               
            #_ = _ax.annotate(str(label), xy=(ys, xs), c="w")
            #_ = _ax.scatter([ys], [xs], s=0.5, c="white")
        plotmasks = False
        if plotmasks:#if CS.pltmasks_bool:
            CS.compute_Masks_to_plot()
            _ = _ax.imshow(CS._masks_cmap(CS._Masks_to_plot[z], alpha=CS._Masks_to_plot_alphas[z], bytes=True), cmap=CS._masks_cmap_name)

class PlotActionCT:
    def __init__(self, fig, ax, CT):
        self.fig=fig
        self.ax=ax
        self.plot_masks=CT.plot_masks
        self.CT=CT
        self.list_of_cells = []
        self.act = fig.canvas.mpl_connect('key_press_event', self)
        self.current_state=None
        self.current_subplot = None
        self.cr = 0
        self.t =0
        self.zs=CT.zs 
        self.z = None
        self.CS = CT.CSt[self.t]
        self.scl = fig.canvas.mpl_connect('scroll_event', self.onscroll)
        if isinstance(self.zs, np.ndarray):
            groupsize  = self.CS.plot_layout[0] * self.CS.plot_layout[1]
            self.max_round =  math.ceil((self.CS.slices)/(groupsize-self.CS.plot_overlap))
        else:
            self.max_round = self.CS.slices
        self.visualization()
        self.update()

    def __call__(self, event):
        if self.current_state==None:
            if event.key == 'd':
                self.current_state="del"
                self.delete_cells()
            elif event.key == 'c':
                self.current_state="com"
                self.combine_cells()
            elif event.key == 'm':
                self.mitosis()
            elif event.key == 'a':
                self.apoptosis()
            elif event.key == 'escape':
                self.visualization()
            self.update()
        else:
            if event.key=='enter':
                if self.current_state=="del":
                    self.CP.stopit()
                    self.CT.delete_cell(self)
                    self.CT.replot_tracking(self.cr)
                    self.list_of_cells = []
                elif self.current_state=="com":
                    self.CP.stopit()
                    self.CT.combine_cells(self)
                    self.CT.cells_to_combine = []
                self.visualization()
                self.update()
                self.current_subplot=None
                self.current_state=None
                self.ax_sel=None
                self.z=None
            else:
                # We have to wait for the current action to finish
                pass

    def onscroll(self, event):
        if self.current_state==None:
            self.current_state="SCL"
            if event.button == 'up':
                self.cr = self.cr + 1
            elif event.button == 'down':
                self.cr = self.cr - 1
            self.cr = max(self.cr, 0)
            self.cr = min(self.cr, self.max_round)
            self.CT.replot_tracking(self.cr)
            self.update()
        else:
            return
        self.current_state=None

    def update(self):
        self.fig.canvas.draw_idle()
        self.fig.canvas.draw()

    def delete_cells(self):
        print("delete")
        self.fig.patch.set_facecolor((1.0,0.0,0.0,0.3))
        self.CP = CellPickerCT_del(self)
    
    def combine_cells(self):
        print("combinations")
        self.fig.patch.set_facecolor((0.0,0.0,1.0,0.3))
        self.CP = CellPickerCT_com(self)

    def mitosis(self):
        print("mitosis")
        self.fig.patch.set_facecolor((0.0,1.0,0.0,0.3))

    def apoptosis(self):
        print("apoptosis")
        self.fig.patch.set_facecolor((0.0,0.0,0.0,0.3))

    def visualization(self):
        print("visualization")
        self.CT.replot_tracking(self.cr)
        self.fig.patch.set_facecolor((1.0,1.0,1.0,1.0))

class CellPickerCT_del():
    def __init__(self, PA):
        self.PA  = PA
        self.cid = self.PA.fig.canvas.mpl_connect('button_press_event', self)
        self.canvas  = self.PA.fig.canvas
    def __call__(self, event):
        if event.button==3:
            if isinstance(self.PA.ax, np.ndarray):
                axshape = self.PA.ax.shape
                # Select ax 
                for i in range(axshape[0]):
                        for j in range(axshape[1]):
                                if event.inaxes==self.PA.ax[i,j]:
                                    self.PA.current_subplot = [i,j]
                                    self.PA.ax_sel = self.PA.ax[i,j]
                                    self.PA.z = self.PA.zs[i,j]
            else:
                self.PA.ax_sel = self.PA.ax

            if event.inaxes!=self.PA.ax_sel:
                print("WRONG AXES")
            else:
                x = np.rint(event.xdata).astype(np.int64)
                y = np.rint(event.ydata).astype(np.int64)
                picked_point = np.array([x, y])
                for i ,mask in enumerate(self.PA.CS.Masks[self.PA.z]):
                    for point in mask:
                        if (picked_point==point).all():
                            z   = self.PA.z
                            lab = self.PA.CS.labels[z][i]
                            cell = [lab, z]
                            #if event.dblclick==True:
                            idx_lab = np.where(np.array(self.PA.CS._Zlabel_l)==lab)[0][0]
                            zs = self.PA.CS._Zlabel_z[idx_lab]
                            add_all=True
                            idxtopop=[]
                            for jj, _cell in enumerate(self.PA.list_of_cells):
                                _lab = _cell[0]
                                _z   = _cell[1]
                                if _lab == lab:
                                    if _z in zs:
                                        add_all=False
                                        idxtopop.append(jj)
                            idxtopop.sort(reverse=True)
                            for jj in idxtopop:
                                self.PA.list_of_cells.pop(jj)
                            if add_all:
                                for zz in zs:
                                    self.PA.list_of_cells.append([lab, zz])
                            self.PA.update()
            print(self.PA.list_of_cells)

            # Select cell and store it   

    def stopit(self):
        self.canvas.mpl_disconnect(self.cid)

class CellPickerCT_com():
    def __init__(self, PA):
        self.PA  = PA
        self.cid = self.PA.fig.canvas.mpl_connect('button_press_event', self)
        self.canvas  = self.PA.fig.canvas
    def __call__(self, event):

        # Button pressed is a mouse right-click (3)
        if event.button==3:

            # Check if the figure is a 2D layout
            if isinstance(self.PA.ax, np.ndarray):
                axshape = self.PA.ax.shape

                # Get subplot of clicked point 
                for i in range(axshape[0]):
                        for j in range(axshape[1]):
                                if event.inaxes==self.PA.ax[i,j]:
                                    self.PA.current_subplot = [i,j]
                                    self.PA.ax_sel = self.PA.ax[i,j]
                                    self.PA.z = self.PA.zs[i,j]
            else:
                raise IndexError("Plot layout not supported")

            # Check if the selection was inside a subplot at all
            if event.inaxes!=self.PA.ax_sel:
                print("WRONG AXES")
            
            # If so, proceed
            else:

                # Get point coordinates
                x = np.rint(event.xdata).astype(np.int64)
                y = np.rint(event.ydata).astype(np.int64)
                picked_point = np.array([x, y])

                # Check if the point is inside the mask of any cell
                for i ,mask in enumerate(self.PA.CS.Masks[self.PA.z]):
                    for point in mask:
                        if (picked_point==point).all():
                            z   = self.PA.z
                            lab = self.PA.CS.labels[z][i]
                            print(self.PA.CT.label_correspondance)
                            idcorr = np.where(np.array(self.PA.CT.label_correspondance[self.PA.t])[:,0]==lab)[0][0]
                            Tlab = self.PA.CT.label_correspondance[self.PA.t][idcorr][1]
                            cell = [Tlab, self.PA.t, idcorr]
                            print("cell = ", cell)
                            # Check if the cell is already on the list
                            if len(self.PA.CT.cells_to_combine)==0:
                                self.PA.CT.cells_to_combine.append(cell)
                            else:
                                if Tlab not in np.array(self.PA.CT.cells_to_combine)[:,0]:
                                    if len(self.PA.CT.cells_to_combine)==2:
                                        print("cannot combine more than 2 cells at once")
                                    else:
                                        if self.PA.t not in np.array(self.PA.CT.cells_to_combine)[:,1]:
                                            self.PA.CT.cells_to_combine.append(cell)
                                else:
                                    self.PA.CT.cells_to_combine.remove(cell)
                            self.PA.update()
            print(self.PA.CT.cells_to_combine)

    def stopit(self):
        
        # Stop this interaction with the plot 
        self.canvas.mpl_disconnect(self.cid)