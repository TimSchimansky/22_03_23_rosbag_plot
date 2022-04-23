import rosbag
import cv2
import numpy as np
import os
from datetime import datetime
import warnings
import pandas as pd
import geopandas as gpd
import open3d as o3d
from io import StringIO
import csv
from shapely.geometry import Point

import matplotlib.pyplot as plt
import seaborn as sns

import map_plotting
from hesai_pandar_64_packets import *
import fix_bag

def vec3_to_list(vector3_in):
    return [vector3_in.x, vector3_in.y, vector3_in.z]


def quaternion_to_list(quaternion_in):
    return [quaternion_in.x, quaternion_in.y, quaternion_in.z, quaternion_in.w]

def polar_to_cartesian(azimuth, elevation, distance):
    elevation = (np.pi / 2) - elevation
    x = distance * np.sin(elevation) * np.cos(azimuth)
    y = distance * np.sin(elevation) * np.sin(azimuth)
    z = distance * np.cos(elevation)
    return x, y, z


class rosbag_reader:
    def __init__(self, bag_file_name):
        """This function is used for initialization"""
        self.source_bag = rosbag.Bag(bag_file_name, 'r')
        self.topics = self.source_bag.get_type_and_topic_info()[1].keys()

        # Keep track of exported topics
        # TODO: add column for comments
        self.exported_data = []

        # Prepare export folder if not existing
        self.bag_unpack_dir = os.path.splitext(os.path.basename(bag_file_name))[0]
        if not os.path.exists(self.bag_unpack_dir):
            os.makedirs(self.bag_unpack_dir)

    def __enter__(self):
        """This function is used for the (with .. as ..) call"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """This function is used to close all connections to files when this class is not needed anymore"""
        self.source_bag.close()

        # Write overview csv for bagfile
        with open(os.path.join(self.bag_unpack_dir, 'overview.csv'), 'w') as f:
            f.write('filename msg_type\n')
            for line in self.exported_data:
                f.write(line + '\n')

    def export_images(self, topic, sensor_name='camera_0'):
        # TODO: make this parameters of the function
        camera_unpack_subdir = sensor_name

        # Prepare export folder if not existing
        export_directory = os.path.join(self.bag_unpack_dir, camera_unpack_subdir)
        if not os.path.exists(export_directory):
            os.makedirs(export_directory)

        for topic, msg, t in self.source_bag.read_messages(topics=[topic]):
            # Convert msg to numpy then to opencv image
            temp_image = cv2.imdecode(np.fromstring(msg.data, np.uint8), cv2.IMREAD_COLOR)

            # Write image into predefined folder
            image_file_name = ("%s.%s.png" % (msg.header.stamp.secs, msg.header.stamp.nsecs))
            cv2.imwrite(os.path.join(export_directory, image_file_name), temp_image)

    def export_pointclouds(self, topic, sensor_name='lidar_0'):
        pcd = o3d.geometry.PointCloud()

        # TODO: make this parameters of the function
        lidar_unpack_subdir = sensor_name

        # Prepare export folder if not existing
        export_directory = os.path.join(self.bag_unpack_dir, lidar_unpack_subdir)
        if not os.path.exists(export_directory):
            os.makedirs(export_directory)

        for msg_number, (topic, msg, t) in enumerate(self.source_bag.read_messages(topics=[topic])):
            # Create empty array for point coordinates and reflectances
            point_array = np.empty((0, 3))
            reflectance_array = np.empty((0, 1))

            # Retrieve sensor calibration from last udp package
            calibration_array_raw = msg.packets[-1].data[8:].decode('utf-8')
            self.calibration_array = np.genfromtxt(StringIO(calibration_array_raw), delimiter=",", skip_header=1)

            for i, packet in enumerate(msg.packets[:-1]):
                # Get cartesian points from UDP packet and append to numpy array
                tmp_point_array, tmp_reflectance_array = self.raw_hesai_to_cartesian(packet.data)

                # Append new data to existing array
                point_array = np.append(point_array, tmp_point_array, axis=0)
                reflectance_array = np.append(reflectance_array, tmp_reflectance_array, axis=0)

                """print(packet.stamp)
                print(msg.packets[0].stamp.secs, packet.stamp.secs)"""

            # Put values into pointcloud object
            pcd.points = o3d.utility.Vector3dVector(point_array)
            pcd.colors = o3d.utility.Vector3dVector(np.repeat(reflectance_array, 3, axis=1) / 255)

            # Export data
            point_cloud_file_name = ("%s.%s.ply" % (msg.packets[0].stamp.secs, msg.packets[0].stamp.nsecs))
            o3d.io.write_point_cloud(os.path.join(export_directory, point_cloud_file_name), pcd)

    def raw_hesai_to_cartesian(self, packet_data):
        # Create data transfer object from binary UDP package
        hesai_udp_dto = HesaiPandar64Packets.from_bytes(packet_data)

        # Pull sensor info from self.calibration array
        elevation_array = (np.pi / 2) - np.deg2rad(self.calibration_array[:, 1])
        azimuth_offset_array = np.deg2rad(self.calibration_array[:, 2])

        # Check for dual return mode
        if hesai_udp_dto.tail.return_mode == 57:
            block_iter_start = 1
            block_iter_step = 2
        else:
            block_iter_start = 0
            block_iter_step = 1

        # Iterate over all blocks
        for block in hesai_udp_dto.blocks[block_iter_start::block_iter_step]:
            # Retrieve azimuth value for current block
            azimuth_rad = np.deg2rad(block.azimuth_deg)
            azimuth_array = azimuth_offset_array + azimuth_rad

            # Get list of all distances and reflectances
            distance_array = np.asarray([channel.distance_value for channel in block.channel])/1000
            reflectance_array = np.asarray([channel.reflectance_value for channel in block.channel])

            # Create mask for zero distance entries
            valid_value_mask = distance_array != 0

            # Conversion from polar to cartesian
            x_array = distance_array[valid_value_mask] * np.sin(elevation_array[valid_value_mask]) * np.cos(azimuth_array[valid_value_mask])
            y_array = distance_array[valid_value_mask] * np.sin(elevation_array[valid_value_mask]) * np.sin(azimuth_array[valid_value_mask])
            z_array = distance_array[valid_value_mask] * np.cos(elevation_array[valid_value_mask])

            return np.vstack((x_array,y_array,z_array)).T, np.expand_dims(reflectance_array[valid_value_mask], axis=1)

    def export_1d_data(self, topic_filter, sensor_name=None):
        """Function to export data from topics that deliver 1 dimensional data"""
        # TODO: allow for multiple topics of same msg type. Current: first gets overwritten
        # Load message type from msg for correct csv translation
        topic_meta = self.source_bag.get_type_and_topic_info(topic_filters=topic_filter)
        message_type = topic_meta.topics[topic_filter].msg_type

        # Debug output
        print('DEBUG: message type of topic: ' + topic_filter + ' is: ' + message_type)

        # Handle file export for barometric pressure data
        if message_type == 'sensor_msgs/FluidPressure':
            # Assemble export filename
            if sensor_name == None:
                export_filename = 'pressure_sensor_0.csv'
            else:
                export_filename = sensor_name + '.csv'
            export_filename = os.path.join(self.bag_unpack_dir, export_filename)

            # TODO: Think about changing this into a binary format (maybe from Pandas)
            # Open file with context handler
            with open(export_filename, 'w') as f:
                # Write header
                f.write('timestamp fluid_pressure variance\n')

                # Iterate over sensor messages
                for topic, msg, t in self.source_bag.read_messages(topics=[topic_filter]):
                    f.write('%.12f %.12f %.12f\n' % (t.to_sec(), msg.fluid_pressure, msg.variance))

        # Handle file export for illuminance data
        elif message_type == 'sensor_msgs/Illuminance':
            # Assemble export filename
            if sensor_name == None:
                export_filename = 'illuminance_sensor_0.csv'
            else:
                export_filename = sensor_name + '.csv'
            export_filename = os.path.join(self.bag_unpack_dir, export_filename)

            # TODO: Think about changing this into a binary format (maybe from Pandas)
            # Open file with context handler
            with open(export_filename, 'w') as f:
                # Write header
                f.write('timestamp illuminance variance\n')

                # Iterate over sensor messages
                for topic, msg, t in self.source_bag.read_messages(topics=[topic_filter]):
                    f.write('%.12f %.12f %.12f\n' % (t.to_sec(), msg.illuminance, msg.variance))

        # Handle file export for IMU data
        elif message_type == 'sensor_msgs/Imu':
            # Assemble export filename
            if sensor_name == None:
                export_filename = 'inertial_measurement_unit_0.csv'
            else:
                export_filename = sensor_name + '.csv'
            export_filename = os.path.join(self.bag_unpack_dir, export_filename)

            # TODO: Think about changing this into a binary format (maybe from Pandas)
            # Open file with context handler
            with open(export_filename, 'w') as f:
                # Write header
                f.write('timestamp or_x or_y or_z or_w li_ac_x li_ac_y li_ac_z an_ve_x an_ve_y an_ve_z\n')

                # Iterate over sensor messages
                for topic, msg, t in self.source_bag.read_messages(topics=[topic_filter]):
                    # Assemble line output by conversion of message into list
                    content_list = [t.to_sec()] + quaternion_to_list(msg.orientation) + vec3_to_list(
                        msg.linear_acceleration) + vec3_to_list(msg.angular_velocity)
                    f.write('%.12f %.12f %.12f %.12f %.12f %.12f %.12f %.12f %.12f %.12f %.12f\n' % tuple(content_list))

        # Handle file export for magnetic field data
        elif message_type == 'sensor_msgs/MagneticField':
            # Assemble export filename
            if sensor_name == None:
                export_filename = 'magnetic_field_sensor_0.csv'
            else:
                export_filename = sensor_name + '.csv'
            export_filename = os.path.join(self.bag_unpack_dir, export_filename)

            # TODO: Think about changing this into a binary format (maybe from Pandas)
            # Open file with context handler
            with open(export_filename, 'w') as f:
                # Write header
                f.write('timestamp x y z\n')

                # Iterate over sensor messages
                for topic, msg, t in self.source_bag.read_messages(topics=[topic_filter]):
                    # Assemble line output by conversion of message into list
                    f.write('%.12f %.12f %.12f %.12f\n' % tuple(
                        [t.to_sec()] + vec3_to_list(msg.magnetic_field)))

        else:
            # TODO: throw exception
            warnings.warn('The topic ' + topic_filter + ' is not available in this bag file!')
            pass

        # Add to list of exported data
        self.exported_data.append(export_filename + ' ' + message_type + ' ' + topic_filter)

class dataframe_with_meta:
    def __init__(self, dataframe, message_type, orig_filename):
        self.dataframe = dataframe
        self.message_type = message_type
        self.orig_filename = orig_filename

class data_as_pandas:
    def __init__(self, directory):
        self.working_directory = directory

        # Load overview csv
        self.data_file_list = pd.read_csv(os.path.join(self.working_directory, 'overview.csv'), sep=' ')
        """for line in self.data_file_list.iterrows():
            print(line[1][0])"""

        # Create empty dict for pandas dataframes
        self.dataframes = dict()

    def load_from_working_directory(self, exclude=None):
        # TODO: implement exclude option

        # Create custom time parser
        unix_time_parser = lambda x: datetime.fromtimestamp(float(x))

        # Iterate over available files
        for data_file in self.data_file_list.iterrows():
            # Assemble file_name and tmp_topic
            file_name = data_file[1][0]
            tmp_topic = data_file[1][1]

            # Load from csv into pandas
            import_path = os.path.join(self.working_directory, file_name)

            # In case of GNSS data create geopandas dataframe
            if tmp_topic == 'sensor_msgs/NavSatFix':
                # tmp_df = dataframe_with_meta(pd.read_csv(import_path, sep=' ', parse_dates=['timestamp'], date_parser=unix_time_parser, index_col='timestamp'), tmp_topic, file_name)
                self.dataframes[tmp_topic] = dataframe_with_meta(pd.read_csv(import_path, sep=' ', parse_dates=['timestamp'], date_parser=unix_time_parser, index_col='timestamp'), tmp_topic, file_name)
                self.dataframes[tmp_topic].dataframe = gpd.GeoDataFrame(self.dataframes[tmp_topic].dataframe, geometry=gpd.points_from_xy(self.dataframes[tmp_topic].dataframe.lon, self.dataframes[tmp_topic].dataframe.lat))

                # Set coordinate system
                self.dataframes[tmp_topic].dataframe.set_crs(epsg=4326, inplace=True)
            else:
                self.dataframes[tmp_topic] = dataframe_with_meta(pd.read_csv(import_path, sep=' ', parse_dates=['timestamp'], date_parser=unix_time_parser, index_col='timestamp'), tmp_topic, file_name)

def dec_2_dms(decimal):
    minute, second = divmod(decimal*3600, 60)
    degree, minute = divmod(minute, 60)
    return '%d° %d\' %.2f\"' %(degree, minute, second)


#fix_bag.fix_bagfile_header("../2022-03-24-11-40-06.bag", "../test3.bag")

# with rosbag_reader("../debug_test_camera_lidar.bag") as reader_object:
with rosbag_reader("../debug_test_camera_lidar.bag") as reader_object:
    print(reader_object.topics)

    #reader_object.export_pointclouds('/hesai/pandar_packets', sensor_name='lidar_0')
    reader_object.export_images('/phone1/camera/image/compressed', sensor_name='camera_0')

    reader_object.export_1d_data('/phone1/android/magnetic_field', sensor_name='magnetic_field_sensor_0')
    reader_object.export_1d_data('/phone1/android/illuminance', sensor_name='illuminance_sensor_0')
    reader_object.export_1d_data('/phone1/android/imu', sensor_name='inertial_measurement_unit_0')
    reader_object.export_1d_data('/phone1/android/barometric_pressure', sensor_name='pressure_sensor_0')

    #reader_object.export_1d_data('/note9/android/barometric_pressure')
    # reader_object.export_1d_data('/phone1/android/illuminance')
    # reader_object.export_1d_data('/phone1/android/imu')
    # reader_object.export_1d_data('/phone1/android/fix')
    #reader_object.export_1d_data('/phone1/android/magnetic_field')
    #reader_object.export_pointclouds()
    # reader_object.export_raw_lidar_data('/hesai/pandar_packets')
    print(reader_object.topics)

"""
bag_pandas = data_as_pandas('kleefeld_trjectory_1')
bag_pandas.load_from_working_directory()

# ---- Testing below --------------------------------------------

# bag_pandas.dataframes['sensor_msgs/MagneticField'].dataframe['timestamp']
# bag_pandas.dataframes['sensor_msgs/NavSatFix'].dataframe['longitude']

# Apply the default theme
sns.set_theme()

mag = bag_pandas.dataframes['sensor_msgs/MagneticField'].dataframe
nav = bag_pandas.dataframes['sensor_msgs/NavSatFix'].dataframe
pre = bag_pandas.dataframes['sensor_msgs/FluidPressure'].dataframe

# Interpolate data
mixed_index = pre.index.join(nav.index, how='outer')
nav_pre = pd.DataFrame(nav.iloc[:,:-1]).reindex(index=mixed_index).interpolate().reindex(pre.index)
nav_pre = gpd.GeoDataFrame(nav_pre, geometry=gpd.points_from_xy(nav_pre.lon, nav_pre.lat))
nav_pre.set_crs(epsg=4326, inplace=True)

# Calculate data boundaries
#left_bound, right_bound = 9.778970033148356, 9.792782600356505
#upper_bound, lower_bound = 52.377849536057006, 52.370752181339995
left_bound, lower_bound, right_bound, upper_bound = nav.total_bounds

# Calculate zoom level from predefined destination width
# TODO: Make destination width a hyper parameter
zoom = map_plotting.determine_zoom_level(left_bound, right_bound, 1000)
map_img, bounding_box = map_plotting.generate_OSM_image(left_bound, right_bound, upper_bound, lower_bound, zoom)

# Plotting
fig, ax = plt.subplots()

# Scatter GNSS data on top
xy = nav_pre.to_crs(epsg=3857).geometry.map(lambda point: point.xy)
x, y = zip(*xy)
ax.scatter(x=x, y=y, c=pre.fluid_pressure)

# Insert image into boundsg
ax.imshow(map_img, extent=(bounding_box.geometry.x[0], bounding_box.geometry.x[1], bounding_box.geometry.y[0], bounding_box.geometry.y[1]))

# Set axes to be equal
#ax.axis('equal')
ax.set_ylim(bounding_box.geometry.y[0], bounding_box.geometry.y[1])
ax.set_xlim(bounding_box.geometry.x[0], bounding_box.geometry.x[1])

# Reformat ticks to epsg:4326
ax.set_xticks(np.linspace(bounding_box.geometry.x[0], bounding_box.geometry.x[1], 5))
xlabel_array = np.linspace(bounding_box.to_crs(epsg=4326).geometry.x[0], bounding_box.to_crs(epsg=4326).geometry.x[1], 5)
xlabel_list = []
for i, xlabel in enumerate(xlabel_array):
    xlabel_list.append(dec_2_dms(xlabel))
ax.set_xticklabels(xlabel_list)

ax.set_yticks(np.linspace(bounding_box.geometry.y[0], bounding_box.geometry.y[1], 4))
ylabel_array = np.linspace(bounding_box.to_crs(epsg=4326).geometry.y[0], bounding_box.to_crs(epsg=4326).geometry.y[1], 4)
ylabel_list = []
for i, ylabel in enumerate(ylabel_array):
    ylabel_list.append(dec_2_dms(ylabel))
ax.set_yticklabels(ylabel_list)

# Show the plot
plt.show()


print(1)

# Interpolate pressure data to magnetic data
mixed_index = mag.index.join(pre.index, how='outer')
pre_mag = pre.reindex(index=mixed_index).interpolate().reindex(mag.index)




# tmp plotting
sns.lineplot(data=mag.x, color="b")
sns.lineplot(data=mag.y, color="g")
sns.lineplot(data=mag.z, color="r")
ax2 = plt.twinx()
sns.lineplot(data=pre_mag.fluid_pressure, color="c", ax=ax2)
plt.show()

print(1)"""
