#!/usr/bin/env python

import rospy
import camproj
import math
import tf
import numpy as np
import scipy.optimize as opt
import shapely.geometry as geom
import heapq
import planar
import roshelper
from tf.transformations import euler_matrix
from tf.transformations import euler_from_quaternion
from tf.transformations import quaternion_from_euler
from geometry_msgs.msg import PoseStamped
from geometry_msgs.msg import Pose
from geometry_msgs.msg import PolygonStamped
from geometry_msgs.msg import Point32
from geometry_msgs.msg import PoseArray
from nav_msgs.msg import Path
from nav_msgs.msg import Odometry
from visualization_msgs.msg import Marker
from visualization_msgs.msg import MarkerArray
from foresight.msg import PolygonArray
from foresight.msg import TreeSearchResultMsg
from foresight.msg import PoseArrayWithTimes
from foresight.msg import ForesightState
from point import Point
from search import SpaceHeapValue
from search import TreeSearchResult


""" Default parameters """
MAP_FRAME = "car/base_link"
QUAD_FRAME = "base_link"
CAM_FRAME = "camera_base_link"

""" Default topics """
BLIND_SPOTS_TOPIC = "/blind_spots"
SCAN_POLYGON_TOPIC = "/bounding_poly"
POSE_ARRAY_TOPIC = "/plan_poses"
PATH_TOPIC = "/plan_path"
OPT_INFO_TOPIC = "/optimization_info"
PROJECTION_MARKERS_TOPIC = "/projection_markers"
POSE_ARRAY_WITH_TIMES_TOPIC = "/waypoints"
SETPOINT_POSE_TOPIC = "/setpoint_pose"
ODOMETRY_TOPIC = "/odometry/filtered"
STATE_TOPIC = "/state"

NODE_NAME = "info_planner"
n = roshelper.Node(NODE_NAME, anonymous=False)


@n.entry_point()
class InfoPlanner(object):

    def __init__(self):
        self.init_camera_projection()
        self.init_planner()
        self.rate = rospy.Rate(rospy.get_param("~frequency", 100))
        self.pose = None
        self.opt_tsr = None
        self.poly = None
        self.last_opt = None
        self.opt_tsr_pubbing = None
        self.added_opt_thresh = 1.0

    def init_planner(self):
        self.perc_opt_thresh = rospy.get_param("~optimality_threshold", 0.7)
        self.max_time = rospy.get_param("~max_execution_time", 5.0)
        self.max_speed = rospy.get_param("~max_speed", 1.0)
        self.timeout = rospy.get_param("~timeout", 0.2)
        self.wait_time = rospy.get_param("~wait_time", 2.0)
        self.buffer_dist = rospy.get_param("~buffer_dist", 0)
        self.step = rospy.get_param("~neighbour_dist", 0.3)
        step = self.step
        self.nbrs = [(step, 0), (0, step), (-step, 0), (0, -step),
                     (step, step), (-step, step), (step, -step),
                     (-step, -step)]
        self.next_pose_dist = rospy.get_param("~next_pose_dist", self.step)
        self.num_nbrs = rospy.get_param("~num_neighbours", 8)
        self.moved_thresh_dist = rospy.get_param("~moved_thresh_dist", 1.0)
        self.moved_thresh_yaw = rospy.get_param(
            "~moved_thresh_yaw", abs(math.sin(math.pi / 5.0)))
        self.last_pose = None
        self.seen_polygon = None
        self.enabled = False
        # self.nbrs = self.get_neighbours()

    def init_camera_projection(self):
        fov_v = rospy.get_param("~fov_v", 0.2 * math.pi)
        fov_h = rospy.get_param("~fov_h", 0.2 * math.pi)
        self.cam = camproj.CameraProjection(fov_v, fov_h)
        self.map_frame = rospy.get_param("~map_frame", MAP_FRAME)
        self.quad_frame = rospy.get_param("~quad_frame", QUAD_FRAME)
        self.camera_frame = rospy.get_param("~camera_frame", CAM_FRAME)
        self.altitude = rospy.get_param("~altitude", 1)
        self.tfl = tf.TransformListener()

    def get_neighbours(self):
        nbrs = list()
        for i in xrange(self.num_nbrs):
            angle = i * 2 * math.pi / self.num_nbrs
            x = self.step * math.cos(angle)
            y = self.step * math.sin(angle)
            nbrs.append((x, y))
        return nbrs

    def update_publishing_path(self, tsr):
        if tsr is None:
            return False

        if self.opt_tsr is None:
            self.opt_tsr = tsr
            return True

        for shv in self.opt_tsr.path:
            if not self.poly.contains(shv.point):
                self.opt_tsr = tsr
                return True

        n_opt = tsr.optimality
        c_opt = self.opt_tsr.optimality
        if n_opt > self.added_opt_thresh * c_opt:
            self.opt_tsr = tsr
            return True

        return False

    @n.subscriber(ODOMETRY_TOPIC, Odometry)
    def odom_sub(self, odom):
        if self.pose is None:
            self.pose = PoseStamped()
            self.last_pose = PoseStamped()
        self.pose.header = odom.header
        self.pose.pose = odom.pose.pose

        dist = self.pose_dist(self.last_pose, self.pose)
        yaw_dist = self.yaw_dist(self.last_pose, self.pose)
        if dist < self.moved_thresh_dist and yaw_dist < self.moved_thresh_yaw:
            self.last_pose = self.pose
            projection = self.get_current_projection()
            proj = np.array(projection).reshape(4, 2)
            proj_poly = geom.Polygon(proj)
            # if self.seen_polygon is None:
            #     self.seen_polygon = proj_poly
            # else:
            #     self.seen_polygon = self.seen_polygon.union(proj_poly)

    @n.main_loop(frequency=30)
    def run(self):
        if self.opt_tsr is not None and self.enabled:
            self.publish_next_pose(self.opt_tsr.path)
            self.publish_pose_array(self.opt_tsr.path)
            self.publish_path(self.opt_tsr.path)
            self.publish_opt_info(self.opt_tsr)
            self.publish_opt_proj_markers(self.opt_tsr.path)

    @n.subscriber(STATE_TOPIC, ForesightState)
    def planner_enabled_sub(self, fs):
        self.enabled = fs.state == ForesightState.PLANNER

    @n.subscriber(SCAN_POLYGON_TOPIC, PolygonStamped, queue_size=1)
    def scan_polygon_cb(self, ps):
        arrs = self.points_to_arrs(ps.polygon.points)
        self.poly = geom.Polygon(arrs).buffer(-self.buffer_dist)

    @n.subscriber(BLIND_SPOTS_TOPIC, PolygonArray, queue_size=1)
    def blind_spots_callback(self, polys):
        multi_polygon = None
        for poly in polys.polygons:
            pts = self.points_to_arrs(poly.points)
            geom_poly = geom.Polygon(pts)
            if multi_polygon is None:
                multi_polygon = geom_poly
            else:
                multi_polygon = multi_polygon.union(geom_poly)
        tsr = self.find_path(multi_polygon)
        self.update_publishing_path(tsr)

    @n.publisher(PolygonStamped)
    def pub_proj(self, proj):
        poly = PolygonStamped()
        poly.header.stamp = rospy.Time.now()
        poly.header.frame_id = self.map_frame
        for v in proj:
            p = Point32()
            p.x = v[0]
            p.y = v[1]
            poly.polygon.points.append(p)
        return poly

    @n.publisher(POSE_ARRAY_TOPIC, PoseArray, queue_size=1)
    def publish_pose_array(self, shvs):
        pa = PoseArray()
        pa.header.stamp = rospy.Time.now()
        pa.header.frame_id = self.map_frame
        for shv in shvs:
            pose = self.point_yaw_to_pose(shv.point, shv.yaw)
            pa.poses.append(pose)
        return pa

    def find_next_pose_in_path(self, shvs):
        min_dist = None
        cur_i = 0
        cur_pt = self.pose_to_geom_point(self.pose)
        for i, shv in enumerate(shvs):
            dist = shv.point.distance(cur_pt)
            if min_dist is None or dist < min_dist:
                min_dist = dist
                cur_i = i
        if cur_i == len(shvs) - 1:
            next_i = cur_i
        else:
            next_i = cur_i + 1
        return cur_pt, next_i

    @n.publisher(SETPOINT_POSE_TOPIC, PoseStamped)
    def publish_next_pose(self, shvs):
        ps = PoseStamped()
        ps.header.frame_id = self.map_frame
        cur_pt, next_i = self.find_next_pose_in_path(shvs)
        cur_pt = self.geom_point_to_vec(cur_pt)
        next_pt = self.geom_point_to_vec(shvs[next_i].point)
        inter_pt = cur_pt + (next_pt - cur_pt).scaled_to(self.next_pose_dist)
        ps.pose = self.point_yaw_to_pose(inter_pt, shvs[next_i].yaw)
        return ps

    @n.publisher(PATH_TOPIC, Path, queue_size=1)
    def publish_path(self, shvs):
        pa = Path()
        pa.header.stamp = rospy.Time.now()
        pa.header.frame_id = self.map_frame
        for shv in shvs:
            ps = PoseStamped()
            ps.header.frame_id = self.map_frame
            ps.header.stamp = rospy.Time.now() + rospy.Duration(shv.ct)
            ps.pose = self.point_yaw_to_pose(shv.point, shv.yaw)
            pa.poses.append(ps)
        return pa

    @n.publisher(OPT_INFO_TOPIC, TreeSearchResultMsg, queue_size=1)
    def publish_opt_info(self, tsr):
        tsr_msg = TreeSearchResultMsg()
        tsr_msg.header.stamp = rospy.Time.now()
        tsr_msg.optimality = tsr.optimality
        tsr_msg.execution_time = tsr.path_exec_time
        tsr_msg.planner_time = tsr.planner_time
        return tsr_msg

    @n.publisher(PROJECTION_MARKERS_TOPIC, MarkerArray)
    def publish_opt_proj_markers(self, shvs):
        markers = MarkerArray()
        for i, shv in enumerate(shvs):
            marker = Marker()
            marker.header.stamp = rospy.Time.now()
            marker.header.frame_id = self.map_frame
            marker.type = Marker.LINE_STRIP
            marker.id = i
            marker.action = Marker.ADD
            marker.scale.x = 0.03
            marker.lifetime = rospy.Duration(0.1)
            marker.color.a = 1.0
            marker.color.g = 0.8
            poly = self.get_projection(
                np.array([shv.point.x, shv.point.y, shv.yaw]))
            for v in poly:
                marker.points.append(self.arr_to_point32(v))
            marker.points.append(self.arr_to_point32(poly[0]))
            markers.markers.append(marker)
        return markers

    def get_residual_polys(self, pt, yaw, polys):
        state = np.array([pt.x, pt.y, yaw])
        projection = self.get_projection(state)
        proj = np.array(projection).reshape(4, 2)
        proj_poly = geom.Polygon(proj)
        if self.seen_polygon is None:
            res_polys = polys.difference(proj_poly)
        else:
            res_polys = polys.difference(proj_poly)\
                .difference(self.seen_polygon)
        return res_polys

    def find_path(self, bs_polys):
        if bs_polys is None or self.pose is None:
            return None
        pt = self.pose_to_geom_point(self.pose)
        first_value = self.make_space_heap_value(pt, bs_polys, 0)
        hq = [first_value]
        parents = dict()
        st = rospy.get_time()
        while len(hq) > 0 and not rospy.is_shutdown():
            shv = heapq.heappop(hq)
            term, res = self.search_terminator(parents, shv, bs_polys, st)
            if term:
                return res
            for nbr_shv in self.propogate_neighbours(shv):
                parents[nbr_shv] = shv
                heapq.heappush(hq, nbr_shv)

    def make_space_heap_value(self, pt, bs_polys, t):
        opt_res = self.find_best_yaw(pt, bs_polys)
        res_polys = self.get_residual_polys(pt, opt_res.x, bs_polys)
        val = SpaceHeapValue(pt, -opt_res.fun, res_polys, t, opt_res.x)
        return val

    def propogate_neighbours(self, shv):
        for nbr in self.nbrs:
            nbr_p = Point(shv.point.x + nbr[0], shv.point.y + nbr[1])
            nt = shv.ct + shv.point.distance(nbr_p) / self.max_speed
            if self.poly.contains(nbr_p) and nt < self.max_time:
                yield self.make_space_heap_value(nbr_p, shv.polys, nt)

    def search_terminator(self, parents, shv, bs_polys, start_time):
        optimality = 1 - shv.polys.area / bs_polys.area
        planner_time = rospy.get_time() - start_time
        path = self.backtrack_path(parents, shv)
        if optimality >= self.perc_opt_thresh or planner_time >= self.timeout:
            tsr = TreeSearchResult(path, optimality, shv.ct, planner_time)
            return True, tsr
        else:
            return False, None

    def backtrack_path(self, parents, shv):
        cur = shv
        path = [shv]
        while cur in parents.keys() and not rospy.is_shutdown():
            cur = parents[cur]
            path.append(cur)
        return list(reversed(path))

    def find_best_yaw(self, pt, polys):
        init = math.pi
        args = (list(pt), polys)
        options = {"disp": False, "maxiter": None}
        kwargs = {"options": options, "method": "Bounded", "args": args,
                  "bounds": (0.0, 2 * math.pi)}
        opt_res = opt.minimize_scalar(self.yaw_objective, init, **kwargs)
        return opt_res

    def yaw_objective(self, yaw, state_2d, polys):
        state = np.array(state_2d + [yaw])
        projection = self.get_projection(state)
        proj = np.array(projection).reshape(4, 2)
        poly = geom.Polygon(proj)
        return -polys.intersection(poly).area

    def get_relative_pose(self, parent_frame, child_frame):
        try:
            self.tfl.waitForTransform(
                parent_frame, child_frame, rospy.Time(),
                rospy.Duration(0.2))
            tr, _ = self.tfl.lookupTransform(
                parent_frame, child_frame, rospy.Time())
            _, quat = self.tfl.lookupTransform(
                child_frame, parent_frame, rospy.Time())
            r, p, y = euler_from_quaternion(quat)
            nquat = quaternion_from_euler(r, p, y)
            ps = PoseStamped()
            ps.pose.position.x = tr[0]
            ps.pose.position.y = tr[1]
            ps.pose.position.z = tr[2]
            ps.pose.orientation.x = nquat[0]
            ps.pose.orientation.y = nquat[1]
            ps.pose.orientation.z = nquat[2]
            ps.pose.orientation.w = nquat[3]
            return ps
        except Exception:
            s = "No transform from {} to {}".format(parent_frame, child_frame)
            rospy.logwarn(s)
        return PoseStamped()

    def get_inverse_pose(self, pose, frame_id):
        pos = pose.pose.position
        quat = pose.pose.orientation
        r, p, y = euler_from_quaternion([quat.x, quat.y, quat.z, quat.w])
        inv_quat = quaternion_from_euler(-r, -p, y)
        inv_pose = PoseStamped()
        inv_pose.header.frame_id = frame_id
        inv_pose.pose.position.x = -pos.x
        inv_pose.pose.position.y = -pos.y
        inv_pose.pose.position.z = -pos.z
        inv_pose.pose.orientation.x = inv_quat[0]
        inv_pose.pose.orientation.y = inv_quat[1]
        inv_pose.pose.orientation.z = inv_quat[2]
        inv_pose.pose.orientation.w = inv_quat[3]
        return inv_pose

    def get_projection(self, state):
        pose_mq = self.state_to_pose(state)
        pose_qm = self.get_inverse_pose(pose_mq, self.quad_frame)
        pose_cq = self.get_relative_pose(self.quad_frame, self.camera_frame)
        rot_qm, trans_qm = self.pose_to_matrix(pose_qm)
        rot_cq, trans_cq = self.pose_to_matrix(pose_cq)
        projection = self.cam.get_projection(rot_qm, rot_cq,
                                             trans_qm, trans_cq)
        return projection

    def get_current_projection(self):
        pose_qm = self.get_relative_pose(self.map_frame, self.quad_frame)
        pose_cq = self.get_relative_pose(self.quad_frame, self.camera_frame)
        rot_qm, trans_qm = self.pose_to_matrix(pose_qm)
        rot_cq, trans_cq = self.pose_to_matrix(pose_cq)
        projection = self.cam.get_projection(rot_qm, rot_cq,
                                             trans_qm, trans_cq)
        return projection

    """ Conversions """

    def state_to_pose(self, state):
        quat = quaternion_from_euler(0, 0, state[2])
        pose_mq = PoseStamped()
        pose_mq.header.frame_id = self.map_frame
        pose_mq.header.stamp = rospy.Time()
        pose_mq.pose.position.x = state[0]
        pose_mq.pose.position.y = state[1]
        pose_mq.pose.position.z = self.altitude
        pose_mq.pose.orientation.x = quat[0]
        pose_mq.pose.orientation.y = quat[1]
        pose_mq.pose.orientation.z = quat[2]
        pose_mq.pose.orientation.w = quat[3]
        return pose_mq

    def point_yaw_to_pose(self, pt, yaw):
        quat = quaternion_from_euler(0, 0, yaw)
        pose_mq = Pose()
        pose_mq.position.x = pt.x
        pose_mq.position.y = pt.y
        pose_mq.position.z = self.altitude
        pose_mq.orientation.x = quat[0]
        pose_mq.orientation.y = quat[1]
        pose_mq.orientation.z = quat[2]
        pose_mq.orientation.w = quat[3]
        return pose_mq

    def pose_dist(self, p, q):
        pvec = planar.Vec2(p.pose.position.x, p.pose.position.y)
        qvec = planar.Vec2(q.pose.position.x, q.pose.position.y)
        return pvec.distance_to(qvec)

    def yaw_dist(self, p, q):
        p_quat = [p.pose.orientation.x,
                  p.pose.orientation.y,
                  p.pose.orientation.z,
                  p.pose.orientation.w]
        q_quat = [q.pose.orientation.x,
                  q.pose.orientation.y,
                  q.pose.orientation.z,
                  q.pose.orientation.w]
        _, _, p_yaw = euler_from_quaternion(p_quat)
        _, _, q_yaw = euler_from_quaternion(q_quat)
        return abs(math.sin(p_yaw - q_yaw))

    def pose_to_geom_point(self, ps):
        return Point(ps.pose.position.x, ps.pose.position.y)

    def geom_point_to_vec(self, pt):
        return planar.Vec2(pt.x, pt.y)

    def points_to_arrs(self, ps):
        arrs = np.zeros((len(ps), 2))
        for i in xrange(len(ps)):
            arrs[i][0] = ps[i].x
            arrs[i][1] = ps[i].y
        return arrs

    def arr_to_point32(self, arr):
        p = Point32()
        p.x = arr[0]
        p.y = arr[1]
        return p

    def pose_to_matrix(self, ps):
        trans = np.matrix([-ps.pose.position.x,
                           -ps.pose.position.y,
                           ps.pose.position.z]).T
        r, p, y = euler_from_quaternion([ps.pose.orientation.x,
                                         ps.pose.orientation.y,
                                         ps.pose.orientation.z,
                                         ps.pose.orientation.w])
        rot = np.matrix(euler_matrix(-r, -p, -y)[:3, :3]).T
        return rot, trans


if __name__ == "__main__":
    n.start(spin=True)
