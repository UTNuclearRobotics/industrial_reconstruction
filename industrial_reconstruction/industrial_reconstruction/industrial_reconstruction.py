# Copyright 2022 Southwest Research Institute
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import cv2
import rclpy
import struct
from rclpy.node import Node
from rclpy.duration import Duration
from rclpy.executors import MultiThreadedExecutor
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.parameter import ParameterType
from rcl_interfaces.msg import ParameterDescriptor
from tf2_ros.buffer import Buffer
from tf2_ros import TransformListener
from std_srvs.srv import Trigger
from industrial_reconstruction_msgs.srv import StartReconstruction, StopReconstruction
import open3d as o3d
import numpy as np
from sensor_msgs.msg import PointCloud2, PointField

from pyquaternion import Quaternion
from collections import deque
from os.path import exists, join, isfile
from sensor_msgs.msg import Image, CameraInfo
from message_filters import ApproximateTimeSynchronizer, Subscriber
from src.industrial_reconstruction.utility.file import make_clean_folder, write_pose, read_pose, save_intrinsic_as_json, make_folder_keep_contents
from industrial_reconstruction_msgs.srv import StartReconstruction, StopReconstruction
from src.industrial_reconstruction.utility.ros import getIntrinsicsFromMsg, meshToRos, transformStampedToVectors

# ROS Image message -> OpenCV2 image converter
from cv_bridge import CvBridge, CvBridgeError
# OpenCV2 for saving an image
from visualization_msgs.msg import Marker

def filterNormals(mesh, direction, angle):
   mesh.compute_vertex_normals()
   tri_normals = np.asarray(mesh.triangle_normals)
   dot_prods = tri_normals @ direction
   mesh.remove_triangles_by_mask(dot_prods < np.cos(angle))
   return mesh

def to_cloud_msg(frame, pointcloud: o3d.geometry.PointCloud, logger=None):
    points  = np.asarray(pointcloud.points)
    colors  = np.asarray(pointcloud.colors)
    normals = np.asarray(pointcloud.normals)

    msg = PointCloud2()
    msg.header.frame_id = frame
    msg.height = 1
    msg.width = points.shape[0]
    msg.is_bigendian = False
    msg.is_dense = False
    data = points
    msg.point_step = 0
    msg.fields = []

    def addField(name):
        nonlocal msg
        msg.fields.append(PointField(name=name, offset=msg.point_step, datatype=PointField.FLOAT32, count=1))
        msg.point_step += 4

    addField("x")
    addField("y")
    addField("z")

    if colors is not None:
        addField("rgb")
        # Convert from floating point tuple (r,g,b) in range [0, 1] to single integer in range [0 255]
        tuple_to_int_rgb = lambda c: 65536*int(c[0]*255) + 256*int(255*c[1]) + int(255*c[2])
        # Packing integer rgb into a float
        int_to_float_rbg = lambda c: struct.unpack('@f', struct.pack('@I', c))

        colors = np.array([int_to_float_rbg(tuple_to_int_rgb(c)) for c in colors], dtype=np.float32)
        data = np.hstack([data, colors])

    if normals is not None:
        addField("normal_x")
        addField("normal_y")
        addField("normal_z")
        data = np.hstack([data, normals])

    msg.row_step = msg.point_step * data.shape[0]
    msg.data = data.astype(np.float32).tostring()
    return msg

class IndustrialReconstruction(Node):

    def __init__(self):
        super().__init__('industrial_reconstruction')

        self.bridge = CvBridge()

        self.buffer = Buffer()
        self.tf_listener = TransformListener(buffer=self.buffer, node=self)

        self.tsdf_volume = None
        self.intrinsics = None
        self.crop_box = None
        self.crop_mesh = False
        self.crop_box_msg = Marker()
        self.tracking_frame = ''
        self.relative_frame = ''
        self.translation_distance = 0.05  # 5cm
        self.rotational_distance = 0.01  # Quaternion Distance

        ####################################################################
        # See Open3d function create_from_color_and_depth for more details #
        ####################################################################
        # The ratio to scale depth values. The depth values will first be scaled and then truncated.
        self.depth_scale = 1000.0
        # Depth values larger than depth_trunc gets truncated to 0. The depth values will first be scaled and then truncated.
        self.depth_trunc = 3.0
        # Whether to convert RGB image to intensity image.
        self.convert_rgb_to_intensity = False

        # Used to store the data used for constructing TSDF
        self.sensor_data = deque()
        self.color_images = []
        self.depth_images = []
        self.rgb_poses = []
        self.prev_pose_rot = np.array([1.0, 0.0, 0.0, 0.0])
        self.prev_pose_tran = np.array([0.0, 0.0, 0.0])

        self.tsdf_integration_data = deque()
        self.integration_done = True
        self.live_integration = False
        self.mesh_pub = None
        self.tsdf_volume_pub = None

        self.record = False
        self.paused = False
        self.frame_count = 0
        self.processed_frame_count = 0
        self.reconstructed_frame_count = 0

        string_type = ParameterDescriptor(type=ParameterType.PARAMETER_STRING)
        self.declare_parameter("depth_image_topic", descriptor=string_type)
        self.declare_parameter("color_image_topic", descriptor=string_type)
        self.declare_parameter("camera_info_topic", descriptor=string_type)
        self.declare_parameter("cache_count", 10)
        self.declare_parameter("slop", 0.01)

        try:
            self.depth_image_topic = (self.get_parameter('depth_image_topic').value)
        except:
            self.get_logger().error("Failed to load depth_image_topic parameter")
        try:
            self.color_image_topic = str(self.get_parameter('color_image_topic').value)
        except:
            self.get_logger().error("Failed to load color_image_topic parameter")
        try:
            self.camera_info_topic = str(self.get_parameter('camera_info_topic').value)
        except:
            self.get_logger().error("Failed to load camera_info_topic parameter")
        try:
            self.cache_count = int(self.get_parameter('cache_count').value)
        except:
            self.get_logger().info("Failed to load cache_count parameter")
        try:
            self.slop = float(self.get_parameter('slop').value)
        except:
            self.get_logger().info("Failed to load slop parameter")
        allow_headerless = False

        self.get_logger().info("depth_image_topic - " + self.depth_image_topic)
        self.get_logger().info("color_image_topic - " + self.color_image_topic)
        self.get_logger().info("camera_info_topic - " + self.camera_info_topic)

        self.depth_sub = Subscriber(self, Image, self.depth_image_topic)
        self.color_sub = Subscriber(self, Image, self.color_image_topic)
        self.tss = ApproximateTimeSynchronizer([self.depth_sub, self.color_sub], self.cache_count, self.slop, allow_headerless)
        self.tss.registerCallback(self.cameraCallback)
        self.info_sub = self.create_subscription(CameraInfo, self.camera_info_topic, self.cameraInfoCallback, 10)

        self.mesh_pub = self.create_publisher(Marker, "industrial_reconstruction_mesh", 10)
        self.cloud_pub = self.create_publisher(PointCloud2, "tsdf_cloud", 10)
        self.tsdf_volume_pub = self.create_publisher(Marker, "tsdf_volume", 10)

        service_group = MutuallyExclusiveCallbackGroup()
        self.start_server = self.create_service(
            StartReconstruction, 'start_reconstruction', self.startReconstructionCallback, callback_group=service_group
        )
        self.stop_server = self.create_service(
            StopReconstruction , 'stop_reconstruction', self.stopReconstructionCallback, callback_group=service_group
        )
        self.pause_server = self.create_service(
            Trigger, 'pause_reconstruction', self.pauseReconstructionCallback, callback_group=service_group
        )
        self.resume_server = self.create_service(
            Trigger, 'resume_reconstruction', self.resumeReconstructionCallback, callback_group=service_group
        )

        reconstruct_frequency = 50.0
        reconstruction_group = MutuallyExclusiveCallbackGroup()
        self.reconstruction_timer = self.create_timer(1/reconstruct_frequency, self.reconstructCallback, callback_group=reconstruction_group)

    def archiveData(self, path_output):
        path_depth = join(path_output, "depth")
        path_color = join(path_output, "color")
        path_pose = join(path_output, "pose")

        make_folder_keep_contents(path_output)
        make_clean_folder(path_depth)
        make_clean_folder(path_color)
        make_clean_folder(path_pose)

        for s in range(len(self.color_images)):
            # Save your OpenCV2 image as a jpeg
            o3d.io.write_image("%s/%06d.png" % (path_depth, s), self.depth_images[s])
            o3d.io.write_image("%s/%06d.jpg" % (path_color, s), self.color_images[s])
            write_pose("%s/%06d.pose" % (path_pose, s), self.rgb_poses[s])
            save_intrinsic_as_json(join(path_output, "camera_intrinsic.json"), self.intrinsics)


    def startReconstructionCallback(self, req: StartReconstruction.Request, res: StartReconstruction.Response):
        self.get_logger().info(" Start Reconstruction")

        self.color_images.clear()
        self.depth_images.clear()
        self.rgb_poses.clear()
        self.sensor_data.clear()
        self.tsdf_integration_data.clear()
        self.prev_pose_rot = np.array([1.0, 0.0, 0.0, 0.0])
        self.prev_pose_tran = np.array([0.0, 0.0, 0.0])

        if (req.tsdf_params.min_box_values.x == req.tsdf_params.max_box_values.x and
                req.tsdf_params.min_box_values.y == req.tsdf_params.max_box_values.y and
                req.tsdf_params.min_box_values.z == req.tsdf_params.max_box_values.z):
            self.crop_mesh = False
        else:
            self.crop_mesh = True
            min_bound = np.asarray(
                [req.tsdf_params.min_box_values.x, req.tsdf_params.min_box_values.y, req.tsdf_params.min_box_values.z])
            max_bound = np.asarray(
                [req.tsdf_params.max_box_values.x, req.tsdf_params.max_box_values.y, req.tsdf_params.max_box_values.z])
            self.crop_box = o3d.geometry.AxisAlignedBoundingBox(min_bound, max_bound)

            self.crop_box_msg.type = Marker.CUBE
            self.crop_box_msg.action = Marker.ADD
            self.crop_box_msg.id = 1
            self.crop_box_msg.scale.x = max_bound[0] - min_bound[0]
            self.crop_box_msg.scale.y = max_bound[1] - min_bound[1]
            self.crop_box_msg.scale.z = max_bound[2] - min_bound[2]
            self.crop_box_msg.pose.position.x = (min_bound[0] + max_bound[0]) / 2.0
            self.crop_box_msg.pose.position.y = (min_bound[1] + max_bound[1]) / 2.0
            self.crop_box_msg.pose.position.z = (min_bound[2] + max_bound[2]) / 2.0
            self.crop_box_msg.pose.orientation.w = 1.0
            self.crop_box_msg.pose.orientation.x = 0.0
            self.crop_box_msg.pose.orientation.y = 0.0
            self.crop_box_msg.pose.orientation.z = 0.0
            self.crop_box_msg.color.r = 1.0
            self.crop_box_msg.color.g = 0.0
            self.crop_box_msg.color.b = 0.0
            self.crop_box_msg.color.a = 0.25
            self.crop_box_msg.header.frame_id = req.relative_frame

            self.tsdf_volume_pub.publish(self.crop_box_msg)

        self.frame_count = 0
        self.processed_frame_count = 0
        self.reconstructed_frame_count = 0

        self.tsdf_volume = o3d.pipelines.integration.ScalableTSDFVolume(
            voxel_length=req.tsdf_params.voxel_length,
            sdf_trunc=req.tsdf_params.sdf_trunc,
            color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8)

        self.depth_scale = req.rgbd_params.depth_scale
        self.depth_trunc = req.rgbd_params.depth_trunc
        self.convert_rgb_to_intensity = req.rgbd_params.convert_rgb_to_intensity
        self.tracking_frame = req.tracking_frame
        self.relative_frame = req.relative_frame
        self.translation_distance = req.translation_distance
        self.rotational_distance = req.rotational_distance

        self.live_integration = req.live
        self.record = True
        self.paused = False

        res.success = True
        return res
    
    def pauseReconstructionCallback(self, req: Trigger.Request, res: Trigger.Response):
        if not self.record:
            res.message = "Cannot pause reconstruction that was never started. Doing nothing"
            res.success = False
            self.get_logger().warn(res.message)
            return res

        self.paused = True
        res.message = "Pausing Reconstruction. "
        if len(self.sensor_data > 0):
            res.message += f"Waiting on {len(self.sensor_data)} to finish processing"
        self.get_logger().info(res.message)
        return res

    def resumeReconstructionCallback(self, req: Trigger.Request, res: Trigger.Response):
        if not self.paused:
            res.message = "Cannot resume reconstruction that was not initially paused. Doing nothing"
            res.success = False
            self.get_logger().warn(res.message)
            return res
        
        self.paused = False
        res.success = True
        res.message = "Resuming reconstruction"
        self.get_logger().info(res.message)
        return res

    def stopReconstructionCallback(self, req: StopReconstruction.Request, res: StopReconstruction.Response):
        self.get_logger().info("Stop Reconstruction")
        self.record = False

        if (len(self.sensor_data) > 0):
            self.get_logger().info("Waiting for all recorded frames to be processed")
        while not (self.integration_done and len(self.sensor_data) == 0):
            self.create_rate(1).sleep()

        self.get_logger().info("Generating mesh")
        if self.tsdf_volume is None:
            res.success = False
            res.message = "Start reconstruction hasn't been called yet"
            return res
        if not self.live_integration:
            while len(self.tsdf_integration_data) > 0:
                data = self.tsdf_integration_data.popleft()
                rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(data[1], data[0], self.depth_scale, self.depth_trunc,
                                                                          False)
                self.tsdf_volume.integrate(rgbd, self.intrinsics, np.linalg.inv(data[2]))
        
        mesh = self.tsdf_volume.extract_triangle_mesh()
        mesh.compute_vertex_normals()

        if self.crop_mesh:
            cropped_mesh = mesh.crop(self.crop_box)
        else:
            cropped_mesh = mesh

        # Mesh filtering
        for norm_filt in req.normal_filters:
            dir = np.array([norm_filt.normal_direction.x, norm_filt.normal_direction.y, norm_filt.normal_direction.z]).reshape(3,1)
            cropped_mesh = filterNormals(cropped_mesh, dir, np.radians(norm_filt.angle))

        triangle_clusters, cluster_n_triangles, cluster_area = (cropped_mesh.cluster_connected_triangles())
        triangle_clusters = np.asarray(triangle_clusters)
        cluster_n_triangles = np.asarray(cluster_n_triangles)
        cluster_area = np.asarray(cluster_area)
        triangles_to_remove = cluster_n_triangles[triangle_clusters] < req.min_num_faces
        cropped_mesh.remove_triangles_by_mask(triangles_to_remove)
        cropped_mesh.remove_unreferenced_vertices()


        o3d.io.write_triangle_mesh(req.mesh_filepath, cropped_mesh, False, True)
        mesh_msg = meshToRos(cropped_mesh)
        mesh_msg.header.stamp = self.get_clock().now().to_msg()
        mesh_msg.header.frame_id = self.relative_frame
        self.mesh_pub.publish(mesh_msg)
        self.get_logger().info(f"Mesh with {len(mesh_msg.points)} points saved to {req.mesh_filepath}")

        if (req.archive_directory != ""):
            self.get_logger().info("Archiving data to " + req.archive_directory)
            self.archiveData(req.archive_directory)
            archive_mesh_filepath = join(req.archive_directory, "integrated.ply")
            o3d.io.write_triangle_mesh(archive_mesh_filepath, mesh, False, True)


        cloud = self.tsdf_volume.extract_point_cloud()
        if cloud.is_empty():
            self.get_logger().warn("Point cloud was empty")
        point_cloud_filepath = join("/".join(req.mesh_filepath.split("/")[:-1]), "integrated_point_cloud.ply")
        o3d.io.write_point_cloud(point_cloud_filepath, cloud)
        self.get_logger().info(f"Point cloud with {len(mesh_msg.points)} points saved to {point_cloud_filepath}")
        self.get_logger().info("DONE")
        res.success = True
        res.message = "Mesh Saved to " + req.mesh_filepath + " and point cloud saved to " + point_cloud_filepath
        return res

    def cameraCallback(self, depth_image_msg, rgb_image_msg):
        if self.paused or not self.record: return

        # Convert your ROS Image message to OpenCV2
        try:
            # TODO: Generalize image type
            cv2_depth_img = self.bridge.imgmsg_to_cv2(depth_image_msg, "16UC1")
            cv2_rgb_img = self.bridge.imgmsg_to_cv2(rgb_image_msg, rgb_image_msg.encoding)
            # Handle grayscale rgb input (TODO: Test this if statement)
            if len(cv2_rgb_img.shape) == 2 and cv2_rgb_img.dtype == np.uint8:
                cv2_rgb_img = cv2.cvtColor(cv2_rgb_img, cv2.COLOR_GRAY2RGB)
        except CvBridgeError as e:
            self.get_logger().error(f"Error converting ros msg to cv img: {e}")
            return
        
        # Get the pose of the camera when this image was taken
        try:
            gm_tf_stamped = self.buffer.lookup_transform( # TODO: Why not invert the transform here?
                self.relative_frame, self.tracking_frame, rgb_image_msg.header.stamp, Duration(seconds=5.0)
            )
        except Exception as e:
            self.get_logger().error(f"Failed to get transform: {e}")
            return
        
        # Record the images along with their pose
        self.sensor_data.append([o3d.geometry.Image(cv2_depth_img), o3d.geometry.Image(cv2_rgb_img), gm_tf_stamped])
        self.frame_count += 1
        
    def reconstructCallback(self):
        if (self.frame_count <= 30 or len(self.sensor_data) == 0): return # TODO: Evaluate necessity of this line

        depth_img, rgb_img, gm_tf_stamped = self.sensor_data.popleft()
        rgb_t, rgb_r = transformStampedToVectors(gm_tf_stamped)
        rgb_r_quat = Quaternion(rgb_r)

        tran_dist = np.linalg.norm(rgb_t - self.prev_pose_tran)
        rot_dist = Quaternion.absolute_distance(Quaternion(self.prev_pose_rot), rgb_r_quat)

        # TODO: Testing if this is a good practice, min jump to accept data
        if (tran_dist >= self.translation_distance) or (rot_dist >= self.rotational_distance):
            self.prev_pose_tran = rgb_t
            self.prev_pose_rot = rgb_r
            rgb_pose = rgb_r_quat.transformation_matrix
            rgb_pose[0, 3] = rgb_t[0]
            rgb_pose[1, 3] = rgb_t[1]
            rgb_pose[2, 3] = rgb_t[2]

            self.depth_images.append(depth_img)
            self.color_images.append(rgb_img)
            self.rgb_poses.append(rgb_pose)

            if self.live_integration and self.tsdf_volume is not None:
                self.integration_done = False
                try:
                    rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
                        rgb_img, depth_img, self.depth_scale, self.depth_trunc, False
                    )
                    
                    self.tsdf_volume.integrate(rgbd, self.intrinsics, np.linalg.inv(rgb_pose)) # TODO: See lookupTransform line
                    self.integration_done = True
                    self.processed_frame_count += 1
                    if self.processed_frame_count % 50 == 0 and self.record:
                        self.get_logger().info("Extracting pointcloud for visualization")
                        cloud = self.tsdf_volume.extract_point_cloud()
                        try:
                            ros_cloud = to_cloud_msg(frame=self.relative_frame, pointcloud=cloud, logger=self.get_logger())
                            ros_cloud.header.stamp = self.get_clock().now().to_msg()
                            self.cloud_pub.publish(ros_cloud)
                        except Exception as E:
                            self.get_logger().warn(f"Error creating cloud: {E}")
                except Exception as e:
                    self.get_logger().error(f"Error processing images into tsdf: {e}")
                    self.integration_done = True
                    return
            else:
                self.tsdf_integration_data.append([depth_img, rgb_img, rgb_pose])
                self.processed_frame_count += 1

    def cameraInfoCallback(self, camera_info):
        self.intrinsics = getIntrinsicsFromMsg(camera_info)


def main(args=None):
    rclpy.init(args=args)
    industrial_reconstruction = IndustrialReconstruction()
    executor = MultiThreadedExecutor(3) 
    executor.add_node(industrial_reconstruction)
    executor.spin()
    industrial_reconstruction.destroy_node()
    rclpy.shutdown()
