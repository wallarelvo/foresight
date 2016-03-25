#!/usr/bin/env python

import rospy
import tf
from tf.transformations import euler_from_quaternion
from apriltags_ros.msg import AprilTagDetectionArray
from geometry_msgs.msg import Quaternion
from geometry_msgs.msg import PoseStamped
from tf.transformations import quaternion_from_euler


NODE_NAME = "apriltags_pose"
FREQUENCY = 30
LANDING_PAD_ID = "/landing_pad"
USB_CAM_ID = "/usb_cam"
ODOM_ID = "/odom"
MAP_ID = "/map"
BASE_LINK_ID = "/base_link"
TAG_DETECTIONS_TOPIC = "/tag_detections"
TF_TIMEOUT = 0.3


class AprilTagsTransformer(object):

    def __init__(self, frequency):
        self.br = tf.TransformBroadcaster()
        self.tfl = tf.TransformListener()
        self.rate = rospy.Rate(frequency)
        self.sub = None
        self.trans = [0, 0, 0]
        self.quat = (0, 0, 0, 1)
        self.lx = None
        self.ly = None
        self.lz = None
        self.lr = 0.7

    def start(self):
        self.sub = rospy.Subscriber(
            TAG_DETECTIONS_TOPIC, AprilTagDetectionArray, self.apriltags_cb)
        self.run()
        return self

    def quat_to_list(self, quat):
        return [quat.x, quat.y, quat.z, quat.w]

    def only_yaw(self, quat, mag=1, add=0):
        _, _, yaw = euler_from_quaternion(self.quat_to_list(quat))
        quat_yaw = quaternion_from_euler(0.0, 0.0, mag * yaw + add)
        ret_quat = Quaternion()
        ret_quat.x = quat_yaw[0]
        ret_quat.y = quat_yaw[1]
        ret_quat.z = quat_yaw[2]
        ret_quat.w = quat_yaw[3]
        return ret_quat

    def apriltags_cb(self, tag_array):
        tags = tag_array.detections
        if len(tags) > 0:
            tag = tags[0]
            ps = tag.pose
            pos = ps.pose.position
            ps.pose.position.x, ps.pose.position.y = pos.y, -pos.x
            ps_bls = self.tfl.transformPose("quad/base_link", ps)
            pos = ps_bls.pose.position
            quat = self.only_yaw(ps_bls.pose.orientation)
            self.br.sendTransform(
                (pos.x, pos.y, pos.z),
                (quat.x, quat.y, quat.z, quat.w),
                rospy.Time.now(), "car/hood_tag", "quad/base_link")
            try:
                t = self.tfl.getLatestCommonTime("odom", "car/hood_tag")
                odom_org = PoseStamped()
                odom_org.header.stamp = t
                odom_org.header.frame_id = "odom"
                tp = self.tfl.transformPose("car/hood_tag", odom_org)
                pos = tp.pose.position
                quat = tp.pose.orientation
                self.quat = self.quat_to_list(self.only_yaw(quat, 1, 3.14))
                if self.lx is None:
                    self.lx = -pos.x
                    self.ly = -pos.y
                    self.lz = pos.z
                # Wrong because I am dumb
                self.lx = (1 - self.lr) * self.lx - self.lr * pos.x
                self.ly = (1 - self.lr) * self.ly - self.lr * pos.y
                self.lz = (1 - self.lr) * self.lz + self.lr * pos.z
                self.trans = (self.lx, self.ly, self.lz)
            except tf.Exception:
                print "TF Error!"

    def run(self):
        while not rospy.is_shutdown():
            self.br.sendTransform(self.trans, self.quat,
                                  rospy.Time.now(),
                                  "odom", "car/hood_link")
            self.rate.sleep()


def main():
    rospy.init_node(NODE_NAME, anonymous=False)
    att = AprilTagsTransformer(100)
    att.start()
    rospy.spin()


if __name__ == "__main__":
    main()
