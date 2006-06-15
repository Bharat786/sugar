from sugar.p2p import network
from sugar.presence.Service import Service

class Notifier:
	TYPE = "_olpc_model_notification._udp"
	ADDRESS = "224.0.0.222"
	PORT = 6300
	
	def __init__(self, group, name):
		service = Service(name, Notifier.TYPE, Notifier.PORT, Notifier.ADDRESS)
		service.register(group)

		address = service.get_group_address()
		port = service.get_port()
		self._client = network.GroupClient(address, port)
		
	def notify(self, msg):
		self._client.send_msg(msg)
