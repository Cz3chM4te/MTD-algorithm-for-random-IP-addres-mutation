from ryu.base import app_manager
from ryu.controller import ofp_event, event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, icmp, arp, ipv4
from ryu.lib import hub
from random import randint,seed
from time import time
import random

class Event(event.EventBase):
    ''' Creating event for upadte ip adresses '''
    def __init__(self, message):
        print("IP Change")
        super(Event, self).__init__()
        self.msg = message

class MTD_ryu(app_manager.RyuApp):
    ''' Basic app configuration '''
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    EV = [Event]
    real_to_virtual_map = {"10.10.10.1": "", "10.10.10.2": ""}
    virtual_to_real_map = {}
    available_ips = {f"10.10.10.{i}" for i in range(10, 22)}
    used_ips = {}
    swap_limit = 3
    swap_count = 0

    def start(self):
        ''' Start the application and spawn a timer thread to generate timeout events '''
        super(MTD_ryu, self).start()
        self.threads.append(hub.spawn(self.timer_for_ip_change))

    def reset_swap_count(self):
        ''' Reset swap count and IP tracking if the limit is reached '''
        if self.swap_count >= self.swap_limit:
            self.swap_count = 0
            self.used_ips = {}
            self.available_ips = {f"10.0.0.{i}" for i in range(10, 22)}

    def timer_for_ip_change(self):
        ''' Manage frequency of vIP change time '''
        while True:
            self.send_event_to_observers(Event("TIMEOUT"))
            sleep_time = random.uniform(10, 30)
            hub.sleep(sleep_time)

    def __init__(self, *args, **kwargs):
        ''' App and variables initialization '''
        super(MTD_ryu, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.datapaths = set()
        self.host_switch_map = {}

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_init(self, ev):
        ''' OVS initialization '''
        datapath = ev.msg.datapath
        OFp = datapath.ofproto
        parser = datapath.ofproto_parser
        self.datapaths.add(datapath)
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(OFp.OFPP_CONTROLLER, OFp.OFPCML_NO_BUFFER)]
        self.set_flow(datapath, 0, match, actions)

    def clear_flow_table(self, datapath):
        ''' Deleting old flows from OVS '''
        OFp = datapath.ofproto
        parser = datapath.ofproto_parser
        match = parser.OFPMatch()
        flow_mod = parser.OFPFlowMod(datapath, 0, 0, 0, OFp.OFPFC_DELETE, 0, 0, 1, OFp.OFPCML_NO_BUFFER, OFp.OFPP_ANY, OFp.OFPG_ANY, 0, match=match, instructions=[])
        datapath.send_msg(flow_mod)

    @set_ev_cls(Event)
    def update_ip_mappings(self, ev):
        ''' Update real-to-virtual IP mappings and clear switch flow tables '''
        self.reset_swap_count()
        available = self.available_ips - set(self.real_to_virtual_map.keys()) - set(self.used_ips.keys())
        self.swap_count += 1

        for real_ip in self.real_to_virtual_map:
            new_virtual_ip = random.choice(list(available - {real_ip}))
            self.used_ips[new_virtual_ip] = new_virtual_ip
            self.real_to_virtual_map[real_ip] = new_virtual_ip
            available.remove(new_virtual_ip)

        self.virtual_to_real_map = {vIP: rIP for rIP, vIP in self.real_to_virtual_map.items()}

        for switch in self.datapaths:
            self.clear_flow_table(switch)
            parser = switch.ofproto_parser
            match = parser.OFPMatch()
            actions = [parser.OFPActionOutput(switch.ofproto.OFPP_CONTROLLER, switch.ofproto.OFPCML_NO_BUFFER)]
            self.set_flow(switch, 0, match, actions)

    def is_real_ip(self, ip):
        ''' Check if the IP address is real '''
        return ip in self.real_to_virtual_map

    def is_virtual_ip(self, ip):
        ''' Check if the IP address is virtual '''
        return ip in self.real_to_virtual_map.values()

    def is_directly_connected(self, datapath, ip):
        ''' Check if the IP is directly connected to the given switch '''
        return self.host_switch_map.get(ip) == datapath if ip in self.host_switch_map else True

    def set_flow(self, datapath, priority, match, actions, buffer_id=None, hard_timeout=None):
        ''' Add a flow entry to a switch '''
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        instructions = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod_args = dict(datapath=datapath, priority=priority, match=match, instructions=instructions)

        if buffer_id is not None:
            mod_args['buffer_id'] = buffer_id
        if hard_timeout is not None:
            mod_args['hard_timeout'] = hard_timeout

        mod = parser.OFPFlowMod(**mod_args)
        datapath.send_msg(mod)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_handler(self, event):
        ''' Handling of incoming packets from OVS '''
        msg = event.msg
        datapath = msg.datapath
        datapath_id = datapath.id
        parser = datapath.ofproto_parser
        incoming_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth_frame = pkt.get_protocol(ethernet.ethernet)
        arp_packet = pkt.get_protocol(arp.arp)
        ipv4_packet = pkt.get_protocol(ipv4.ipv4)
        actions = []
        drop_packet = False

        if arp_packet:
            src_ip, dst_ip = arp_packet.src_ip, arp_packet.dst_ip

            if self.is_real_ip(src_ip) and src_ip not in self.host_switch_map:
                self.host_switch_map[src_ip] = datapath.id

            if self.is_real_ip(src_ip):
                flow_match = parser.OFPMatch(eth_type=0x0800,in_port=incoming_port,ipv4_src=src_ip,ipv4_dst=dst_ip)
                actions.append(parser.OFPActionSetField(arp_spa=self.real_to_virtual_map[src_ip]))

            if self.is_virtual_ip(dst_ip):
                flow_match = parser.OFPMatch(eth_type=0x0800,in_port=incoming_port,ipv4_src=src_ip,ipv4_dst=dst_ip)
                if self.is_directly_connected(datapath.id, self.virtual_to_real_map[dst_ip]):
                    actions.append(parser.OFPActionSetField(arp_tpa=self.virtual_to_real_map[dst_ip]))
                else:
                    drop_packet = True
            elif not self.is_directly_connected(datapath.id, dst_ip):
                drop_packet = True

        elif ipv4_packet:
            src_ip, dst_ip = ipv4_packet.src, ipv4_packet.dst

            if self.is_real_ip(src_ip) and src_ip not in self.host_switch_map:
                self.host_switch_map[src_ip] = datapath.id

            if self.is_real_ip(src_ip):
                flow_match = parser.OFPMatch(eth_type=0x0800,in_port=incoming_port,ipv4_src=src_ip,ipv4_dst=dst_ip)
                actions.append(parser.OFPActionSetField(ipv4_src=self.real_to_virtual_map[src_ip]))

            if ipv4_packet.proto == in_proto.IPPROTO_UDP:
                pkt_udp = pkt.get_protocol(udp.udp)
                data = msg.data
                self._handler_dns(datapath,eth_frame,incoming_port,ipv4_packet,pkt_udp,data)

            if self.is_virtual_ip(dst_ip):
                flow_match = parser.OFPMatch(eth_type=0x0800,in_port=incoming_port,ipv4_src=src_ip,ipv4_dst=dst_ip)
                if self.is_directly_connected(datapath.id, self.virtual_to_real_map[dst_ip]):
                    actions.append(parser.OFPActionSetField(ipv4_dst=self.virtual_to_real_map[dst_ip]))
                else:
                    drop_packet = True

        self.mac_to_port.setdefault(datapath_id, {})
        self.mac_to_port[datapath_id][eth_frame.src] = incoming_port

        dst_port = self.mac_to_port[datapath_id].get(eth_frame.dst, datapath.ofproto.OFPP_FLOOD)
        if not drop_packet:
            actions.append(parser.OFPActionOutput(dst_port))

        if dst_port != datapath.ofproto.OFPP_FLOOD:
            self.set_flow(datapath, 1, flow_match, actions, msg.buffer_id)

        packet_out_message = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=incoming_port,
            actions=actions,
            data=msg.data
        )
        datapath.send_msg(packet_out_message)
