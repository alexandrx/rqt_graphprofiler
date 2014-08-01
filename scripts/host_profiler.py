#!/usr/bin/env python
import yappi
import psutil
import numpy as np
from threading import *
import types

import rospy
import rosgraph
import rosnode
import xmlrpclib
from rqt_graphprofiler.msg import *
from rosgraph_msgs.msg import *

from callback_timer import CallbackTimer

SILENT_TOPICS = ['/graphprofile','/statistics','/tf','/rosout','/rosout_agg']

class HostProfiler(object):
    """ Collects profile information for this host and the ROS nodes running on it """
    def __init__(self):
        self._hostname = rosgraph.network.get_host_name()
        self._ipaddress = rosgraph.network.get_local_address()
        rospy.init_node('HostProfiler_%s'%rosgraph.network.get_host_name())
        self._master = rosgraph.Master('HostProfiler_%s'%self._hostname)
        self._lock = Lock()
        self._subscriber = rospy.Subscriber('/statistics',TopicStatistics, self._topic_statistics_callback)
        self._publisher = rospy.Publisher('/graphprofile',HostProfile, queue_size = 10)

        self._publisher_timer = CallbackTimer(1.1,self._publish_profile)

        # Processes we are watching
        self._nodes = dict()

    def start(self):
        """ Start the HostProfiler """
        # Get an up-to-date list of the node processes we want to be monitoring
        self._update_nodes_list()
        # First start each nodes individual monitor, then start the publication timer
        for node in self._nodes.values():
            node.start()
        self._publisher_timer.start()

    def stop(self):
        """ Stop the HostProfiler """
        # Stop the publisher timer first, since its callback and restart a stopped
        # node monitor.
        self._publisher_timer.stop()
        for node in self._nodes.values():
            node.stop()

    def _publish_profile(self):
        """ Publish profile information collected by this host """
        with self._lock:
            # Populate information collected about this host
            host = HostProfile()
            host.hostname = self._hostname
            host.ipaddress = self._ipaddress

            # Populate information collected for each node
            # Stop All Nodes
            for node_monitor in self._nodes.values():
                node_monitor.stop()

            print "Publishing %d nodes"%len(self._nodes.values())
            print "Publishing Monitors:",len([node.published_topics for node in self._nodes.values()])
            print "Subscribing Monitors:",len([node.subscribed_topics for node in self._nodes.values()])
            # Process Information about each node and add it to the HostProfile message
            for node_monitor in self._nodes.values():
                nodeprofile = node_monitor.get_profile()
                host.nodes.append(nodeprofile)
                node_monitor.reset()
                    
            # Send out the data!
            self._publisher.publish(host)
            print "---"

            # Update information about the system state (topology) for use in the
            # next collection period
#             self._update_nodes_list()

            # Restart all nodes
            for node_monitor in self._nodes.values():
                node_monitor.start()

            self.start_time = self._publisher_timer.restart()

    def _update_nodes_list(self):
        """ Updates the list of nodes on this machine that we want to collect information about """
        # Remove Node monitors for processes that no longer exist
        for name in self._nodes.keys():
            if not self._nodes[name].is_running():
                print "Removing Monitor for '%s'"%name
                self._nodes.pop(name)

        # Add node monitors for nodes on this machine we are not already monitoring
        nodenames = rosnode.get_nodes_by_machine(rosgraph.network.get_host_name())
        for name in nodenames:
            if not name in self._nodes:
                print "Adding Monitor for '%s'"%name
                node = NodeMonitor()
                node.name = name
                try:
                    # Get the uri
                    node.uri = self._master.lookupNode(name)
                except rosgraph.masterapi.MasterError:
                    print "WARNING: MasterAPI Error"
                try:
                    # The the pid
                    node_api = rosnode.get_api_uri(rospy.get_master(), name)
                    code, msg, node.pid = xmlrpclib.ServerProxy(node_api[2]).getPid('/NODEINFO')
                except xmlrpclib.socket.error:
                    print "WANRING: XML RPC ERROR"
                # TODO: It would be nice to get the 'type' and 'package' information too
                self._nodes[name] = node

        # Get create a monitor for every topic on each node we are monitoring. 
        # TODO: for each monitor clear the publishers and subscribers list?
        for node in self._nodes.values():
            node.published_topics = dict()
            node.subscribed_topics = dict()

        # Compile a dictionary of topic names to types
        topic_types = dict()
        for name, type_ in self._master.getTopicTypes():
            topic_types[name] = type_

        systemstate = self._master.getSystemState()
        for topic_name, publisher_list in systemstate[0]:
            if topic_name in SILENT_TOPICS:
                continue
            for publishername in publisher_list:
                if publishername in self._nodes:
                    monitor = TopicMonitor()
                    monitor.topic = topic_name
                    monitor.type_ = topic_types[topic_name]
                    self._nodes[publishername].published_topics[topic_name] = monitor
        for topic_name, subscribers_list in systemstate[1]:
            if topic_name in SILENT_TOPICS:
                continue
            for subscribername in subscribers_list:
                if subscribername in self._nodes:
                    monitor = TopicMonitor()
                    monitor.topic = topic_name
                    monitor.type_ = topic_types[topic_name]
                    self._nodes[subscribername].subscribed_topics[topic_name] = monitor


    def _topic_statistics_callback(self,data):
        """ record statistics message into TopicMonitors for the publisher and the subscriber """
        if data.topic in SILENT_TOPICS:
            return
        with self._lock:
            print "received topic statistics for",data.topic,data.node_pub,data.node_sub
            # If we are monitoring the node that is named as the publisher, record
            # the data in that NodeMonitors' TopicMonitor for that particular topic.
            if data.node_pub in self._nodes:
                node = self._nodes[data.node_pub]
                if data.topic not in node.published_topics:
                    rospy.logerr("WARNING: Topic %s not in node %s's publisher list"%(data.topic,data.node_pub))
                else:
                    monitor = node.published_topics[data.topic]
                    monitor.save_statistics(data)
            # If we are monitoring the node that is named as the subscriber, record
            # the data in that NodeMonitors' TopicMonitor for that particular topic.
            if data.node_sub in self._nodes:
                node = self._nodes[data.node_sub]
                if data.topic not in node.subscribed_topics:
                    rospy.logerr("WARNING: Topic %s not in node %s's subscriber list"%(data.topic,data.node_sub))
                else:
                    monitor = node.subscribed_topics[data.topic]
                    monitor.save_statistics(data)
         

class NodeMonitor(object):
    """ Tracks process statistics of a PID using an internal timing mechanism"""
    def __init__(self):
        self.name = None
        self.uri = None
        self.pid = None
        self._process = None
        self._interval_timer = CallbackTimer(0.2, self._update_callback)

        self.cpu_log = list()
        self.virt_log = list()
        self.res_log = list()
        self.published_topics = dict() # name: TopicMonitor
        self.subscribed_topics = dict() # name: TopicMonitor
        self.start_time = None
        self.stop_time = None

    def start(self):
        """ Start the process monitor """
        if self._process is None:
            try:
                self._process = psutil.Process(int(self.pid))
            except:
                if not isinstance(self.pid,int):
                    raise Exception("PID for process %s not defined"%self.name)
        self.reset()
        self.start_time = self._interval_timer.start()

    def stop(self):
        """ Stop the process monitor """
        self.stop_time = self._interval_timer.stop()
      
    def is_running(self):
        if self._process is None:
            return False
        return self._process.is_running()

    def _update_callback(self):
        try:
            self.cpu_log.append(self._process.get_cpu_percent())
            virt, real = self._process.get_memory_info()
            self.virt_log.append(virt)
            self.res_log.append(real)
            self._interval_timer.restart()
        except psutil.NoSuchProcess:
            rospy.logerr("WARNING: Lost Node Monitor for '%s'"%self.name)

    def get_profile(self):
        """ Returns a NodeProfile() """
        node = NodeProfile()
        node.name = self.name
        node.pid = self.pid

        if len(self.cpu_log) > 0:
            cpu_log = np.array(self.cpu_log)
            node.cpu_load_mean = np.mean(cpu_log)
            node.cpu_load_std = np.std(cpu_log)
            node.cpu_load_max = np.max(cpu_log)
        if len(self.virt_log) > 0:
            virt_log = np.array(self.virt_log)
            node.virt_mem_mean = np.mean(virt_log)
            node.virt_mem_std = np.std(virt_log)
            node.virt_mem_max = np.max(virt_log)
        if len(self.res_log) > 0:
            res_log = np.array(self.res_log)
            node.real_mem_mean = np.mean(res_log)
            node.real_mem_std = np.std(res_log)
            node.real_mem_max = np.max(res_log)

        # Evaluate the topic information
        for topicmonitor in self.published_topics.values():
            topicprofile = topicmonitor.get_profile()
            node.published_topics.append(topicprofile)
            topicmonitor.reset()
        for topicmonitor in self.subscribed_topics.values():
            topicprofile = topicmonitor.get_profile()
            node.subscribed_topics.append(topicprofile)
            topicmonitor.reset()
        return node

    def reset(self):
        self.cpu_log = list()
        self.virt_log = list()
        self.res_log = list()
#         self.published_topics = dict() # name: TopicMonitor
#         self.subscribed_topics = dict() # name: TopicMonitor
        self.start_time = None
        self.stop_time = None




class TopicMonitor(object):
    """ Collect and combine TopicStatistics information for an anonymous publisher or subscriber. 

    TopicStatistic messages are for pairwise connections and are not representative of the 
    full MIMO topic. We may receive multiple or none during a time period, depending on
    the publishing rate of the HostProfiler since Topic Statistics always publish at 1Hz.
    Also note that some topics may be publishing very slowly or irregularly. 

    A single TopicStatistic message will be recorded in two TopicMonitors (one for the publisher
    and one for the subscriber), although these are not necessarily on the same Host. 
    """
    def __init__(self):
        self.topic = None
        self.type_ = None
        self.num_connections = 0
        self._has_data = False

        self.dropped_msgs = list()
        # Observed Traffic, in bytes
        self.traffic = list()

        # period between two messages
        self.period_mean = list()
        self.period_stddev = list()
        self.period_max = list()

        # age of messages
        self.stamp_age_mean = list()
        self.stamp_age_stddev = list()
        self.stamp_age_max = list()

    @property
    def has_data(self):
        return self._has_data

    def save_statistics(self, data):
        """ Record TopicStatistcs() message for profiling """
        assert(isinstance(data, TopicStatistics))
        assert(self.topic == data.topic)
        self._has_data = True
        self.dropped_msgs.append(data.dropped_msgs)
        self.traffic.append(data.traffic)
        self.period_mean.append(data.period_mean)
        self.period_stddev.append(data.period_stddev)
        self.period_max.append(data.period_max)
        self.stamp_age_mean.append(data.stamp_age_mean)
        self.stamp_age_stddev.append(data.stamp_age_stddev)
        self.stamp_age_max.append(data.stamp_age_max)

    def get_profile(self):
        """ Report combined Statistics Information from this monitor as a TopicProfile() """
        topic = TopicProfile()
        topic.topic = self.topic
        topic.type = self.type_

        # Return now if we have not received any information
        if not self._has_data:
            return topic

        topic.dropped_msgs = sum(self.dropped_msgs)
        topic.traffic = sum(self.traffic)
        mean = np.mean(np.array([d.to_sec() for d in self.period_mean]))
        topic.period_mean = rospy.Duration(mean if not np.isnan(mean) else 0)
        # topic.period_stddev  
        max_ = max([d.to_sec() for d in self.period_max])
        topic.period_max = rospy.Duration(max_ if not np.isnan(max_) else 0)

        mean = np.mean(np.array([d.to_sec() for d in self.stamp_age_mean]))
        topic.stamp_age_mean = rospy.Duration(mean if not np.isnan(mean) else 0)
        # topic.stamp_age_stddev
        max_ = max([d.to_sec() for d in self.stamp_age_max])
        topic.stamp_age_max = rospy.Duration(max_ if not np.isnan(max_) else 0)
        return topic

    def reset(self):
        self.num_connections = 0
        self._has_data = False
        self.dropped_msgs = list()
        self.traffic = list()
        self.period_mean = list()
        self.period_stddev = list()
        self.period_max = list()
        self.stamp_age_mean = list()
        self.stamp_age_stddev = list()
        self.stamp_age_max = list()




def sizeof_fmt(num):
    # Taken from http://stackoverflow.com/a/1094933
    for x in ['bytes','KB','MB','GB','TB']:
        if num < 1024.0:
            return "%3.1f %s" % (num, x)
        num /= 1024.0



if __name__ == "__main__":
    yappi.start()
    profiler = HostProfiler()
    profiler.start()
    rospy.spin()
#     r = rospy.Rate(10)
#     try:
#         while not rospy.is_shutdown():
#             r.sleep()
#     except rospy.ROSInterruptException:
#         pass
    profiler.stop()
    yappi.get_func_stats().print_all(columns={0:("name",120), 1:("ncall",5), 2:("tsub", 8), 3:("ttot",8), 4:("tavg",8)})
