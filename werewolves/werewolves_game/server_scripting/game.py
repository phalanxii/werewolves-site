''' game.py - handles Game()

	game.state = ['voting', 'lobby', 'discussion', 'starting']

	Event
	Game
	User
		Player
			Character
				Werewolf, Human, Witch, Mystic_etc
		Meta information (score, friends)
'''
import warnings
import redis
import uuid
import json
import weakref
from random import shuffle
from math import ceil

from werewolves_game.server_scripting.redis_util import *		# for the ww_redis_handler. Redundant for that purpose, it should be found in the swampdragon file redis_publisher
import werewolves_game.server_scripting.user as user
from werewolves_game.server_scripting.characters import Human, Werewolf, Witch, Character, CharacterFactory
import werewolves_game.server_scripting as wwss
import werewolves_game.server_scripting.event as event

from tornado.ioloop import PeriodicCallback, IOLoop
from swampdragon.pubsub_providers.data_publisher import publish_data

from importlib import import_module
from django.conf import settings
SessionStore = import_module(settings.SESSION_ENGINE).SessionStore


class GameManager:
	def __init__(self):
		return

	# allocate a player to a game

class Game:
	def __init__(self, g_id=None, session_key=None):
		self.name = "Richard's room"
		self.g_round = 0
		self.players = []	# list of Players
		self.state = "matchmaking"	# default. Currently unused
		self.saved = False		# async issues?
		self.callback_queue = {}	# handlers for async 
		self.event_queue = []	# events waiting to happen. Triggered based on Game.state
		self.event_history = []	# past events
		self.options = {	'max_players': 2,
							'fortune_teller': False,
							'witch': False	}

		if g_id is None and session_key is not None:
			session_data = SessionStore(session_key=session_key)
			if 'g_id' in session_data:
				g_id = session_data['g_id']

		try:
			self.load(g_id)
		except Exception as error:
			print(error)
			print("creating default game values")
			self.g_id = str(uuid.uuid4())
			self.state = "lobby"
			self.g_round = 0

		self.iol = IOLoop.current()	# main tornado loop uses for callbacks

	def __del__(self):
		self.characters = {}
		self.players = []

	def __eq__(self, other):
		return self.g_id == other.g_id

	def save(self):
		ww_redis_db.hset("g_list:"+self.g_id, "name", self.name)
		ww_redis_db.hset("g_list:"+self.g_id, "round", self.g_round)
		ww_redis_db.hset("g_list:"+self.g_id, "state", self.state)
		
		players_string = ""
		if len(self.players) > 0:
			players_string = "|".join(self.get_player_ids())
		ww_redis_db.hset("g_list:"+self.g_id, "players", players_string)

		print("events are messy and there's duplicate code here. Event handler class needed as component")
		
		cur_events_string = ""
		if self.event_queue:
			cur_events_string = "|".join([event.e_id for event in self.event_queue])
		ww_redis_db.hset("g_list:"+self.g_id, "curevents", cur_events_string)
		
		old_events_string = ""
		if self.event_history:
			old_events_string = "|".join([event.e_id for event in self.event_history])
		ww_redis_db.hset("g_list:"+self.g_id, "curevents", old_events_string)

		for event in self.event_queue:
			event.save()

		for event in self.event_history:
			event.save()

		for player in self.players:
			player.save()

		games_dict = {}
		data_dict = {}
		games_dict["json"] = self.as_JSON()

		data_dict["games"] = games_dict
		data_dict["channel"] = "game:"+self.g_id

		publish_data("game:"+self.g_id, data_dict)

		self.saved = True

	def load(self, g_id):
		if g_id is None:
			raise Exception("No g_id supplied")

		redis_game = ww_redis_db.hgetall("g_list:"+str(g_id))
		redis_game = {k.decode('utf8'): v.decode('utf8') for k, v in redis_game.items()}

		if not redis_game:
			raise Exception("g_list:"+str(g_id)+" was not found in redis")

		self.g_id = g_id
		if redis_game['players'] != "":
			player_ids = redis_game['players'].split('|')
			for p_id in player_ids:
				if p_id not in self.get_player_ids():
					player = user.Player(p_id)
					self.players.append(CharacterFactory.create_character(player.character, p_id=player.p_id))
		else:
			self.players = []

		self.state = redis_game['state']

		return True

	def as_JSON(self):
		game_json = {}
		game_json['g_id'] = self.g_id
		game_json['name'] = self.name
		game_json['g_round'] = self.g_round

		players_json = {}
		for player in self.players:
			players_json[player.p_id] = player.as_JSON()

		game_json['players'] = players_json
		game_json['state'] = self.state
		game_json['history'] = self.event_history

		return json.dumps(game_json, default=lambda o: o.__dict__, sort_keys=True, indent=4)

	def delete(self):
		ww_redis_db.delete("g_list:"+self.g_id)

	# in here in case I want to chain together multiple operations, avoiding multiple DB calls. More faff than it's worth?
	def is_saved(self):
		if self.saved:
			return True
		else:
			return False

	def get_players(self):
		return self.players

	def get_player_ids(self):
		return [player.p_id for player in self.players]

	def get_group(self, selectors):
		# loop through self.players and remove those that don't fit the group as an array
		group_list = self.players

		for selector in selectors:
			# selecting based on player.state
			if selector in ("alive", "dead", "dying"):
				group_list = [player for player in group_list if player.state == selector]

			# selecting based on last event
			elif selector == "last_result":
				group_list = [player for player in group_list if player in self.event_history[0].result_subjects]

			# selecting based on Class type
			elif issubclass(selector, Character):
				group_list = [player for player in group_list if isinstance(player, selector)]

			# selecting based on Class type from string
			elif isinstance(selector, str):
				for player in self.players:
					print("no more eval for you")
					#selector = eval(selector)
					#if not isinstance(player, selector):
						#group_list.remove(player)
					raise TypeError

		return group_list

	def assign_roles(self):
		shuffle(self.players)
		werewolves_count = ceil(0.3*self.options['max_players'])

		if self.options['witch']:
			witch_count = ceil(0.1*self.options['max_players'])
		else:
			witch_count = 0

		for x in range(werewolves_count):
			self.players[x].character = "werewolf"

		for x in range(werewolves_count, werewolves_count+witch_count):
			self.players[x].character = "witch"

		for x in range(werewolves_count+witch_count, len(self.players)):
			self.players[x].character = "human"

		for i, player in enumerate(self.players):
			self.players[i] = CharacterFactory.create_character(player.character, p_id=player.p_id)
			self.players[i].game = weakref.ref(self)	# raises errors if actions done on characters not through the game
			self.players[i].save()
			print(player.character)

	def change_state(self, state):
		self.state = state

		if state == "ready":
			print("game starting")
			self.assign_roles()
			self.change_state("waiting")
			return

		if state == "waiting":	# if no other events, start a new round
			print("nothing happening. Starting a new night/day cycle")
			self.g_round += 1
			night = event.EventFactory.create_event("night", self)
			self.event_queue.append(night)

			warnings.warn("day event not done yet")
			# day = EventFactory.create_event("day", self)
			# self.event_queue.append(day)
			self.change_state("new_event")
			return

		if state == "new_event":
			# publish data to players
			print("new event starting: " + self.event_queue[0].e_type)
			self.event_queue[0].start()
			warnings.warn("NOT broadcasted to user")

		if state == "finished_voting":
			print("Votes collected, performing result")
			self.event_queue[0].finish_event()
			raise NotImplementedError

		if state == "game_finished":
			#save to DB, kick all players etc.
			print("Game over")
			raise NotImplementedError

		data_dict = {}
		data_dict['state'] = state
		data_dict['channel'] = "game:"+self.g_id

		publish_data("game:"+self.g_id, data_dict)

	def game_end(self):
		winners = ["humans", "werewolves"]

		if not self.get_group([Human, "alive"]):
			winners.remove("werewolves")

		if not self.get_group([Werewolf, "alive"]):
			winners.remove("humans")

		return winners

	def add_player(self, p_id=None, player=None):
		if p_id:
			player = user.Player(p_id)
		if player not in self.players:
			# add a weakref to game
			player.game_instance = weakref.ref(self)
			self.players.append(player)


		if len(self.players) >= self.options['max_players']:
			print("Game full, starting now")
			self.change_state("ready")

		self.save()

	def remove_player(self, p_id=None):
		if p_id:
			player = user.Player(p_id)

		while player in self.players:
			self.players.remove(player)
		
		self.save()



# should use tasks scheduler programs for this (ie Celery), not global functions, but fine for development
pcb = None

def broadcast_games():
	global pcb
	data_dict = {}
	games_dict = {}

	if pcb is None:
		pcb = PeriodicCallback(broadcast_games, 6000)
		pcb.start()

	g_list = ww_redis_db.keys("g_list:*")
	for v in g_list:
		v = v.decode("utf-8")

		if len(g_list) > 0:
			# find game with least spaces
			for g_key in g_list:
				g_id = str(g_key.decode("utf-8")).split(":")[1]
				game = Game(g_id)
				
				games_dict["json"] = game.as_JSON()

	data_dict["games"] = games_dict
	data_dict["channel"] = "lobbyinfo"

	publish_data("lobbyinfo", data_dict)
	return data_dict

def push_system_message(msg):
	data_dict = {}
	data_dict["message"] = msg

	data_dict["channel"] = "sysmsg"

	publish_data("sysmsg", data_dict)
	return data_dict

pcb2 = None
cur_g_id = None

# redundant and is broken
def broadcast_players(g_id):
	global pcb2
	global cur_g_id
	data_dict = {}

	if pcb2 is None:
		cur_g_id = g_id
		pcb2 = PeriodicCallback(lambda: broadcast_players(g_id), 4000)
		pcb2.start()
	elif cur_g_id != g_id:
		cur_g_id = g_id
		pcb2.stop()
		pcb2 = PeriodicCallback(lambda: broadcast_players(g_id), 4000)
		pcb2.start()

	g_list = ww_redis_db.keys(g_id+"*")
	for v in g_list:
		v = v.decode("utf-8")

		if len(g_list) > 0:
			# find game with least spaces
			for g_key in g_list:
				game = ww_redis_db.hgetall(g_key)
				game = {k.decode('utf8'): v.decode('utf8') for k, v in game.items()}	#convert from byte array to string dict

				players = game['players'].split("|")		# obtain players in current game in the form of uuid

				data_dict[g_key.decode("utf-8")] = str(len(players))

	data_dict["channel"] = "player_list"

	publish_data("player_list:"+g_id, data_dict)

	return data_dict

pcb3 = None
def check_activity():
	global pcb3

	if pcb3 is None:
		pcb3 = PeriodicCallback(lambda: check_activity(), 10000)
		pcb3.start()

	sessions = Session.objects.all()

	for s in sessions:
		print(s.get_decoded())

	return
