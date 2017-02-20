import os
import sys
import time
import shlex
import shutil
import inspect
import aiohttp
import discord
import asyncio
import traceback
import random
import requests
import untangle
import json
import wikipedia
import pytz
import difflib

from cleverwrap import CleverWrap
from PyDictionaryMod import PyDictionaryMod
from datetime import datetime, timedelta

from discord import utils
from discord.object import Object
from discord.enums import ChannelType
from discord.ext.commands.bot import _get_variable

from io import BytesIO
from functools import wraps
from textwrap import dedent
from datetime import timedelta
from random import choice, shuffle
from collections import defaultdict

from bs4 import BeautifulSoup as bs

from kiyubot.config import Config, ConfigDefaults
from kiyubot.permissions import Permissions, PermissionsDefaults
from kiyubot.utils import load_file, write_file, sane_round_int

from . import exceptions
from .constants import VERSION as BOTVERSION
from .constants import DISCORD_MSG_CHAR_LIMIT

from microsofttranslator import Translator

class SkipState:
    def __init__(self):
        self.skippers = set()
        self.skip_msgs = set()

    @property
    def skip_count(self):
        return len(self.skippers)

    def reset(self):
        self.skippers.clear()
        self.skip_msgs.clear()

    def add_skipper(self, skipper, msg):
        self.skippers.add(skipper)
        self.skip_msgs.add(msg)
        return self.skip_count


class Response:
    def __init__(self, content, reply=False, delete_after=0):
        self.content = content
        self.reply = reply
        self.delete_after = delete_after


class KiyuBot(discord.Client):
    def __init__(self, config_file=ConfigDefaults.options_file, perms_file=PermissionsDefaults.perms_file):
        super().__init__()

        self.players = {}
        self.locks = defaultdict(asyncio.Lock)
        self.aiosession = aiohttp.ClientSession(loop=self.loop)
        self.config = Config(config_file)
        self.permissions = Permissions(perms_file, grant_all=[self.config.owner_id])
        self.exit_signal = None
        
        # Initialise PyDictionaryMod
        self.dictionary = PyDictionaryMod()
        
        # Initialise Microsoft translations
        self.translator = Translator(self.config.microsoftkey, self.config.microsoftsecret)
        
        # Initialise PSO2 chanel list
        self.pso2_channels = load_file('kiyubot/resources/pso2.txt')
        
        # Initialise Cleverbot
        self.cleverwrap = CleverWrap("CCCjbJptJO7_nJWmpx0EBFSyIxw")
        
        # Create pso2_channels (empty list) if it doesn't exist
        if len(self.pso2_channels) == 0:
            self.pso2_channels = []
            
        # Add bound pso2 channels to the list if there are any
        if len(self.config.pso2_channels) > 0:
            self.pso2_channels += self.config.pso2_channels.split()
            
        # Initialise pso2 values
        self.pso2_previous_message_text = None

        self.http.user_agent += ' KiyuBot/%s' % BOTVERSION

        ssd_defaults = {'last_np_msg': None, 'auto_paused': False}
        self.server_specific_data = defaultdict(lambda: dict(ssd_defaults))
        

    def owner_only(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            # Only allow the owner to use these commands
            orig_msg = _get_variable('message')

            if not orig_msg or orig_msg.author.id == self.config.owner_id:
                return await func(self, *args, **kwargs)
            else:
                raise exceptions.PermissionsError("Only the owner can use this command", expire_in=30)

        return wrapper

    @staticmethod
    def _fixg(x, dp=2):
        return ('{:.%sf}' % dp).format(x).rstrip('0').rstrip('.')

    def _get_owner(self):
        return discord.utils.find(lambda m: m.id == self.config.owner_id, self.get_all_members())

    async def _wait_delete_msg(self, message, after):
        await asyncio.sleep(after)
        await self.safe_delete_message(message)

    async def _manual_delete_check(self, message, *, quiet=False):
        if self.config.delete_invoking:
            await self.safe_delete_message(message, quiet=quiet)

    async def safe_send_message(self, dest, content, *, tts=False, expire_in=0, also_delete=None, quiet=False):
        msg = None
        try:
            msg = await self.send_message(dest, content, tts=tts)

            if msg and expire_in:
                asyncio.ensure_future(self._wait_delete_msg(msg, expire_in))

            if also_delete and isinstance(also_delete, discord.Message):
                asyncio.ensure_future(self._wait_delete_msg(also_delete, expire_in))

        except discord.Forbidden:
            if not quiet:
                self.safe_print("Warning: Cannot send message to %s, no permission" % dest.name)

        except discord.NotFound:
            if not quiet:
                self.safe_print("Warning: Cannot send message to %s, invalid channel?" % dest.name)

        return msg

    async def safe_delete_message(self, message, *, quiet=False):
        try:
            return await self.delete_message(message)

        except discord.Forbidden:
            if not quiet:
                self.safe_print("Warning: Cannot delete message \"%s\", no permission" % message.clean_content)

        except discord.NotFound:
            if not quiet:
                self.safe_print("Warning: Cannot delete message \"%s\", message not found" % message.clean_content)

    async def safe_edit_message(self, message, new, *, send_if_fail=False, quiet=False):
        try:
            return await self.edit_message(message, new)

        except discord.NotFound:
            if not quiet:
                self.safe_print("Warning: Cannot edit message \"%s\", message not found" % message.clean_content)
            if send_if_fail:
                if not quiet:
                    print("Sending instead")
                return await self.safe_send_message(message.channel, new)

    def safe_print(self, content, *, end='\n', flush=True):
        sys.stdout.buffer.write((content + end).encode('utf-8', 'replace'))
        if flush: sys.stdout.flush()

    async def send_typing(self, destination):
        try:
            return await super().send_typing(destination)
        except discord.Forbidden:
            if self.config.debug_mode:
                print("Could not send typing to %s, no permssion" % destination)

    async def edit_profile(self, **fields):
        if self.user.bot:
            return await super().edit_profile(**fields)
        else:
            return await super().edit_profile(self.config._password,**fields)

    def _cleanup(self):
        try:
            self.loop.run_until_complete(self.logout())
        except: # Can be ignored
            pass

        pending = asyncio.Task.all_tasks()
        gathered = asyncio.gather(*pending)

        try:
            gathered.cancel()
            self.loop.run_until_complete(gathered)
            gathered.exception()
        except: # Can be ignored
            pass

    # noinspection PyMethodOverriding
    def run(self):
        try:
            self.loop.run_until_complete(self.start(*self.config.auth))

        except discord.errors.LoginFailure:
            # Add if token, else
            raise exceptions.HelpfulError(
                "Bot cannot login, bad credentials.",
                "Fix your Email or Password or Token in the options file.  "
                "Remember that each field should be on their own line.")

        finally:
            try:
                self._cleanup()
            except Exception as e:
                print("Error in cleanup:", e)

            self.loop.close()
            if self.exit_signal:
                raise self.exit_signal

    async def logout(self):
        return await super().logout()

    async def on_error(self, event, *args, **kwargs):
        ex_type, ex, stack = sys.exc_info()

        if ex_type == exceptions.HelpfulError:
            print("Exception in", event)
            print(ex.message)

            await asyncio.sleep(2)
            await self.logout()

        elif issubclass(ex_type, exceptions.Signal):
            self.exit_signal = ex_type
            await self.logout()

        else:
            traceback.print_exc()
            
    async def on_ready(self):
        print('\rConnected!  KiyukiiBot v%s\n' % BOTVERSION)
        if self.config.owner_id == self.user.id:
            raise exceptions.HelpfulError(
                "Your OwnerID is incorrect or you've used the wrong credentials.",

                "The bot needs its own account to function.  "
                "The OwnerID is the id of the owner, not the bot.  "
                "Figure out which one is which and use the correct information.")

        self.safe_print("Bot:   %s/%s#%s" % (self.user.id, self.user.name, self.user.discriminator))

        owner = self._get_owner()
        if owner and self.servers:
            self.safe_print("Owner: %s/%s#%s\n" % (owner.id, owner.name, owner.discriminator))

            print('Server List:')
            [self.safe_print(' - ' + s.name) for s in self.servers]

        elif self.servers:
            print("Owner could not be found on any server (id: %s)\n" % self.config.owner_id)

            print('Server List:')
            [self.safe_print(' - ' + s.name) for s in self.servers]

        else:
            print("Owner unavailable, bot is not on any servers.")
            # if bot: post help link, else post something about invite links

        print()

        if self.config.bound_channels:
            chlist = set(self.get_channel(i) for i in self.config.bound_channels if i)
            chlist.discard(None)
            invalids = set()

            print("Bound to text channels:")
            [self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in chlist if ch]
            print()

        else:
            print("Not bound to any text channels")

        print("\nOptions:")

        self.safe_print("  Command prefix: " + self.config.command_prefix)
        print("  Delete Messages: " + ['Disabled', 'Enabled'][self.config.delete_messages])
        if self.config.delete_messages:
            print("  Delete Invoking: " + ['Disabled', 'Enabled'][self.config.delete_invoking])
        print("  Debug Mode: " + ['Disabled', 'Enabled'][self.config.debug_mode])
        print()
        
        # Resume sending EQs in PSO2 enabled channels
        # Remove dead channels first
        pso2_channels_pruned = [id for id in self.pso2_channels if not self.get_channel(id=id) == None]
        
        # Update file if items were pruned
        if len(pso2_channels_pruned) < len(self.pso2_channels):
            self.pso2_channels = pso2_channels_pruned
            write_file('kiyubot/resources/pso2.txt', self.pso2_channels)
            print("Pruned dead channel(s).")
            
        # Notify enabled channels about restart (or start)
        for channel_id in self.pso2_channels:
            channel = self.get_channel(channel_id)
            await self.safe_send_message(channel, "Kiyu restarted and will continue watching for EQs in `#%s`.\n" % (channel.name)) 
            
        # Start watching EQs, even if there are no enabled channels
        # This is so -pso2 will return the last displayed alert
        await self.pso2_watcher()
        
    async def cmd_pso2(self, message):
        """
        Usage:
            {command_prefix}pso2 on|off

        Turns on pso2 EQ notifications for the channel.
        """
        query = message.content.replace(self.config.command_prefix + 'pso2', '').strip()

        if query == "on":
            # Add current channel if it's not on the list
            if message.channel.id not in self.pso2_channels:
                self.pso2_channels.append(message.channel.id)
                # Save in file
                write_file('kiyubot/resources/pso2.txt', self.pso2_channels)
                await self.safe_send_message(message.channel, "Kiyu will now watch for EQs and tell you in `#%s`.\n" 
                    % (message.channel.name))
            # Notify if current channel is already on the list
            else:
                await self.safe_send_message(message.channel, "Kiyu is already watching for EQs in `#%s`!" % (message.channel.name))
        elif query == "off":
            # Remove current channel if it's on the list
            if message.channel.id in self.pso2_channels:
                self.pso2_channels.remove(message.channel.id)
                # Save in file
                write_file('kiyubot/resources/pso2.txt', self.pso2_channels)
                await self.safe_send_message(message.channel, "Kiyu is no longer watching for EQs in `#%s` anymore.\n" 
                    % (message.channel.name))
            # Current channel isn't on the list
            else:
                await self.safe_send_message(message.channel, \
                    "Kiyu isn't even watching for any EQs in `#%s` in the first place!!" % (message.channel.name))
        # No valid argument, just return the current EQ alert
        else:
            current_time_utc = pytz.utc.localize(datetime.utcnow())
            timezone_time = current_time_utc.astimezone(pytz.timezone("Japan"))
            
            """
            Format is:
            Current Time
            23:00 JST
            """
            
            # Split the time string so we can strip zeroes off the hour and day
            time_string = "```Current Time\n" + timezone_time.strftime('%H:%M') + " JST```"
            await self.safe_send_message(message.channel, self.pso2_previous_message_text + "```\n" + time_string)
        
    async def pso2_watcher(self):
        """
        Periodically checks for PSO2 Emergency Quest updates
        Used by cmd_pso2()
        """
        url = 'http://pso2emq.flyergo.eu/api/v2/'
        
        while True:
            try:
                response = requests.get(url)
                
                # Something went wrong with fetch
                if response.status_code != 200:
                    await asyncio.sleep(220)
                    continue
                
                json_object = json.loads(response.text)
                eq_text = "```%s" % (json_object[0]["text"])
            except Exception:
                # Wait 220 seconds first before continuing
                await asyncio.sleep(220)
                continue
            
            if self.pso2_previous_message_text != eq_text:
                self.pso2_previous_message_text = eq_text
                
                # Find and append current JST time to EQ text
                current_time_utc = pytz.utc.localize(datetime.utcnow())
                timezone_time = current_time_utc.astimezone(pytz.timezone("Japan"))
                
                """
                Format is:
                Current Time
                23:00 JST
                """
                
                # Split the time string so we can strip zeroes off the hour and day
                time_string = "```Current Time\n" + timezone_time.strftime('%H:%M') + " JST```"
                
                # Append current JST time to EQ text
                eq_text += "```\n" + time_string
                
                # Remove dead channels first
                pso2_channels_pruned = [id for id in self.pso2_channels if not self.get_channel(id=id) == None]
                
                # Update file if items were pruned
                if len(pso2_channels_pruned) < len(self.pso2_channels):
                    self.pso2_channels = pso2_channels_pruned
                    write_file('kiyubot/resources/pso2.txt', self.pso2_channels)
                    print("Pruned dead channel(s).")
                
                # Send the notification to all registered servers
                for channel in self.pso2_channels:
                    await self.safe_send_message(self.get_channel(id=channel), eq_text)    
            # Wait 220 seconds between checks
            await asyncio.sleep(220)
        
            
    async def cmd_pusheen(self):
        """
        Usage:
            {command_prefix}pusheen

        Sends a pusheen gif
        """
        
        # Pusheen gifs are obtained from a giphy API
        response = requests.get('http://api.giphy.com/v1/gifs/random?api_key=dc6zaTOxFJmzC&tag=pusheen')
        
        if response.status_code != 200:
            # This means something went wrong.
            return Response("Kiyu couldn't get any Pusheen gifs.")
            
        json_object = json.loads(response.text)
        
        return Response("Pusheeeeeen ♫\n" + json_object["data"]["image_original_url"])  
        
    async def cmd_bunny(self):
        """
        Usage:
            {command_prefix}bunny

        Sends a bunny gif
        """
        
        # Bunny gifs are obtained from a giphy API
        response = requests.get('http://api.giphy.com/v1/gifs/random?api_key=dc6zaTOxFJmzC&tag=bunny')
        
        if response.status_code != 200:
            # This means something went wrong.
            return Response("Kiyu couldn't get any bunny gifs.")
            
        json_object = json.loads(response.text)
        
        return Response("_Squeeks_\n" + json_object["data"]["image_original_url"])  
        
    async def cmd_dict(self, message):
        """
        Usage:
            {command_prefix}dict (term)

        Looks up a term on the dictionary
        """
        
        term = message.content.replace(self.config.command_prefix + 'dict', '').strip().title()
        
        # No term specified
        if len(term) == 0:
            return Response("Kiyu needs something to look up!")
        
        # Lookup term with Dictionary
        dict_lookup = self.dictionary.meaning(term)
        
        if dict_lookup == None:
            return Response("Kiyu couldn't find a definition for `%s`. Are you sure you typed it right?" % (term))
        
        """ 
        Result looks like this:
        {'Noun': ['a concave cut into a surface or edge (as in a coastline', 'the
        formation of small pits in a surface as a consequence of corrosion', 'the
        space left between the margin and the start of an indented line', 'the 
        act of cutting into an edge with toothlike notches or angular incisions']}         
        """
        
        result_string = ":mag:**%s**:\n" % (term)
        for type in dict_lookup:
            result_string += "\n_%s_\n" % (type)
            term_number = 1
            for definition in dict_lookup[type]:
                result_string += "\t%d. %s\n" % (term_number, definition)
                term_number += 1
        
        return Response(result_string)
    
    async def cmd_urban(self, message):
        """
        Usage:
            {command_prefix}urban (phrase)

        Finds a phrase in urban dictionary
        """
        query = message.content.replace(self.config.command_prefix + 'urban', '').strip()
        
        # No term specified
        if len(query) == 0:
            return Response("Kiyu needs something to look up!")
        
        # Replace spaces with %20 for lookup only
        url = "http://www.urbandictionary.com/define.php?term={}".format(query.replace(' ', '%20'))
        
        with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                r = await resp.read()
        resp = bs(r,'html.parser')
        try:
            if len(resp.find('div', {'class':'meaning'}).text.strip('\n').replace("\u0027","'")) >= 1000:
                meaning = resp.find('div', {'class':'meaning'}).text.strip('\n').replace("\u0027","'")[:1000] + "..."
            else:
                meaning = resp.find('div', {'class':'meaning'}).text.strip('\n').replace("\u0027","'")
            return Response(":mag:**{0}**: \n{1}\n\n**Example**: \n{2}\n\n**~{3}**"
                .format(query,meaning,resp.find('div', {'class':'example'}).text.strip('\n'),resp
                .find('div', {'class':'contributor'}).text.strip('\n')), delete_after=20)
        except AttributeError:
            return Response("Kiyu couldn't find an entry for `%s`. Are you sure you typed it right?" % (query))
                
    async def cmd_wiki(self, message):
        """
        Usage:
            {command_prefix}wiki (phrase)

        Looks up term in wikipedia
        """
        query = message.content.replace(self.config.command_prefix + 'wiki', '').strip().title()
        
        if len(query) == 0:
            return Response("Kiyu needs something to search for!")
                
        try:
            wiki_page = wikipedia.page(query)
            result_string = ":mag:**%s**:\n%s" % \
                (wiki_page.title, wikipedia.summary(query, sentences=5).replace('\n', '\n\n'))
        except wikipedia.exceptions.PageError:
            return Response("Kiyu couldn't find a wiki page for `%s`." % query)
        except wikipedia.exceptions.DisambiguationError as e:
            # Seperate disambiguation entries by semicolon, instead of \n
            error_string = str(e).replace('\n', '', 1).replace('\n', '; ')
            
            # Chop off end of string if the length is too long
            if len(error_string) > 1900:
                error_string = error_string[:1900]
                error_string = error_string[:error_string.rfind(';')]
            return Response("Kiyu found too many entries for `%s`. Please be more specific!\n```%s```" \
                % (query, error_string))
        
        return Response(result_string)
        
    async def cmd_translate(self, message):
        """
        Usage:
            {command_prefix}translate (phrase)

        Translates something into English
        """
        query = message.content.replace(self.config.command_prefix + 'translate', '').strip().title()
        
        if len(query) == 0:
            return Response("Kiyu needs something to translate!")
                
        try:
            translated_phrase = self.translator.translate(query, "en")
            return Response(translated_phrase)
        except Exception:
            return Response("Kiyu couldn't translate it for some reason. Are you sure Kiyu was set up correctly!?")
        
        
        
    async def cmd_time(self, message):
        """
        Usage:
            {command_prefix}time [timezone]

        Tells the current time in a timezone
        """
        query = message.content.replace(self.config.command_prefix + 'time', '').strip()
        
        # Default to Japan
        if len(query) == 0:
            query = "Japan"
        
        # Check for input (title) first, then check all caps
        closest_match = difflib.get_close_matches(query.title(), pytz.all_timezones)
        if len(closest_match) == 0:
            closest_match = difflib.get_close_matches(query.upper(), pytz.all_timezones)
            # Remove "Etc/" from results and run again
            for i in range(len(closest_match)):
                closest_match[i] = closest_match[i].replace('Etc/', '')
            closest_match = difflib.get_close_matches(query.upper(), closest_match)
        
        # If no timezones were found
        if len(closest_match) == 0:
            return Response("Kiyu doesn't know any timezones that look like `%s`." % (query))
            
        timezone = closest_match[0]
        
        try:
            current_time_utc = pytz.utc.localize(datetime.utcnow())
            timezone_time = current_time_utc.astimezone(pytz.timezone(timezone))
            
            """
            Format is:
            10:00 AM
            Monday, January 5, 2016
            Time in GMT
            """
            
            # Split the time string so we can strip zeroes off the hour and day
            time_string = "```" + timezone_time.strftime('%I:%M %p').lstrip('0') \
                                + timezone_time.strftime('\n%A, %B ') \
                                + timezone_time.strftime('%d, %Y').lstrip('0') \
                                + "\nTime in %s```" % (timezone)
            return Response(time_string)
        except pytz.UnknownTimeZoneError:
            return Response("Kiyu doesn't know any timezones that look like `%s`." % (query))
            
    async def cmd_penguin(self):
        """
        Usage:
            {command_prefix}penguin

        Sends Penguins
        """
        url = "http://penguin.wtf/"
        with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                r = await resp.read()
        resp = bs(r,'html.parser')
        return Response("Pingu pingu!\n" + resp.text, delete_after=20)
        
    async def cmd_dot(self):
        """
        Usage:
            {command_prefix}dot

        Makes bot send a dot
        """

        return Response(".")   
        
    async def cmd_heart(self):
        """
        Usage:
            {command_prefix}heart

        Makes Kiyukii-Bot send a heart
        """

        return Response("<3")  
        
    async def cmd_8ball(self):
        """
        Usage:
            {command_prefix}8ball

        Ask Kiyu a yes or no question
        """
    
        # 8ball replies are located in this file
        data = load_file('kiyubot/resources/8ball.txt')
        return Response(random.choice(data)) 
                
    async def cmd_tsun(self):
        """
        Usage:
            {command_prefix}tsun

        Makes bot be tsundere
        """
        
        # Tsundere lines are located in this file
        data = load_file('kiyubot/resources/tsun.txt')

        return Response(random.choice(data)) 
    
    async def cmd_kiyu(self):
        """
        Usage:
            {command_prefix}kiyu

        Kiyu pictures!
        """
        
        # Links to Kiyu pictures are located in this file
        data = load_file("kiyubot/resources/kiyu.txt", "r") 
        return Response("Kiyu ♪♪\n" + random.choice(data)) 
        
    async def cmd_azu(self):
        """
        Usage:
            {command_prefix}azu

        Azu pictures!
        """
        
        # Links to Azu pictures are located in this file
        data = load_file("kiyubot/resources/azu.txt", "r") 
        return Response("Azu〜\n" + random.choice(data)) 
        
    async def cmd_honk(self):
        """
        Usage:
            {command_prefix}honk

        Sends a Chen honk picture
        """
            
        # Links to Chen pictures are located in this file
        data = load_file("kiyubot/resources/honk.txt", "r")
        return Response("Honk honk!\n" + random.choice(data))
        
    async def cmd_cat(self):
        """
        Usage:
            {command_prefix}cat

        Sends a cat picture
        """
        
        # Cat pictures are obtained from an API
        response = requests.get('http://thecatapi.com/api/images/get?format=xml&results_per_page=1')
        
        if response.status_code != 200:
            # This means something went wrong.
            return Response("Kiyu couldn't get any cat picture.")
        
        xml = untangle.parse(response.text)
        return Response("Nyaa〜\n" + xml.response.data.images.image.url.cdata)  
        
    async def cmd_safebooru(self, message):
        """
        Usage:
            {command_prefix}safebooru [optional query]

        Sends a random picture from safebooru
        """
        
        query = message.content.replace(self.config.command_prefix + 'safebooru', '').strip()
        
        # Form URL based on whether or not a query was input
        url = 'http://safebooru.org/index.php?page=dapi&s=post&q=index%s' % \
            ('' if len(query) == 0 else '&tags=' + query.replace(' ', '+'))
        xml = untangle.parse(url)
        
        # Maximum results = 100
        post_count = int(xml.posts['count'])
        num_results = 100 if post_count >= 100 else post_count
        
        # No results
        if num_results == 0:
            return Response("Kiyu couldn't find any pictures for `%s`." % query)
        
        try:
            return Response('http:' + xml.posts.post[random.randint(0, num_results-1)]['file_url'])  
        # Type returned probably NoneType
        except TypeError:
            return Response("Kiyu couldn't find any pictures for `%s`." % query)
            
    async def cmd_gelbooru(self, message):
        """
        Usage:
            {command_prefix}gelbooru [optional query]

        Sends a random picture from gelbooru
        """
        
        query = message.content.replace(self.config.command_prefix + 'gelbooru', '').strip()
        
        # Form URL based on whether or not a query was input
        url = 'http://gelbooru.com/index.php?page=dapi&s=post&q=index%s' % \
            ('' if len(query) == 0 else '&tags=' + query.replace(' ', '+'))
        xml = untangle.parse(url)
        
        # Maximum results = 100
        post_count = int(xml.posts['count'])
        num_results = 100 if post_count >= 100 else post_count
        
        # No results
        if num_results == 0:
            return Response("Kiyu couldn't find any pictures for `%s`." % query)
        
        try:
            return Response(xml.posts.post[random.randint(0, num_results-1)]['file_url'])  
        # Type returned probably NoneType
        except TypeError:
            return Response("Kiyu couldn't find any pictures for `%s`." % query)

    async def cmd_timer(self, message):
        """
        Usage:
            {command_prefix}timer [seconds] [reminder]

        Sets a timer for a user with the option of setting a reminder text.
        """
        
        query = message.content.replace(self.config.command_prefix + 'timer', '').strip()
        query_split = query.split(' ', 1)
        
        try:
            # Test if seconds given was a number
            seconds = int(query_split[0])
            
            try:
                # Attempt assuming there's a reminder message
                remember = query_split[1]
                endtimer = self.safe_send_message(
                    message.channel, message.author.mention + ", your timer for " + str(seconds) + 
                        " seconds has expired! Kiyu was instructed to remind you about `" + remember + "`!")
                await self.safe_send_message(
                    message.channel, message.author.mention + ", Kiyu will remind you about `" + 
                        remember + "` in " + str(seconds) + " seconds!")
                await asyncio.sleep(float(seconds))
                await endtimer
            except IndexError:
                # I guess there wasn't a reminder message
                endtimer = self.safe_send_message(
                    message.channel, message.author.mention + ", your timer for " + str(seconds) + " seconds has expired!")
                await self.safe_send_message(
                    message.channel, message.author.mention + ', you have set a timer for ' + str(seconds) + ' seconds!')
                await asyncio.sleep(float(seconds))
                await endtimer
        except ValueError:
            # Seconds given wasn't a number
            return Response("That's not what Kiyu expected. The format is `%stimer [seconds] " \
                "[optional reminder message]`" % (self.config.command_prefix))
        
    async def cmd_choice(self, message):
        """
        Usage:
            {command_prefix}choice choice1;choice2;choice3

        Makes bot choose one of the choices
        """
        
        choices = message.content.replace(self.config.command_prefix + 'choice', '').strip()
        if len(choices) == 0:
            return Response("Kiyu doesn't see any choices to make.")

        choices = choices.split(';')
        return Response("Kiyu chooses **%s**!" % (random.choice(choices)))    

    async def cmd_coinflip(self, message):
        """
        Usage:
            {command_prefix}coinflip

        Makes bot flip a coin
        """
        
        return Response("Kiyu flipped a **%s**." % (random.choice(['heads', 'tails'])))        
    
    async def cmd_roll(self, message):
        """
        Usage:
            {command_prefix}roll #

        Makes bot roll a #-sided die (defaults to 6 if no number given)
        """
        
        sides = message.content.replace(self.config.command_prefix + 'roll', '').strip()
        
        try:
            sides = int(sides)
            
            # User tries to roll less than 1 sided
            if sides < 1:
                return Response("Kiyu doesn't know what you wanted to roll, so she rolled a 6-sided die instead.\n" \
                    "Kiyu rolled a **%d**." % (random.randint(1, 6)))
            
            # User tries to roll higher than 1000 sides
            if sides > 1000:
                return Response("Too many sides. Kiyu rolled a 1000-sided die instead.\n" \
                    "Kiyu rolled a **%d**." % (random.randint(1, 1000)))
            # Normal case (1-1000)
            return Response("Kiyu rolled a **%d**." % (random.randint(1, sides)))
        except ValueError:
            return Response("Kiyu doesn't know what you wanted to roll, so she rolled a 6-sided die instead.\n" \
                "Kiyu rolled a **%d**." % (random.randint(1, 6)))
    
    async def cmd_rr(self, message):
        """
        Usage:
            {command_prefix}rr

        Allows the user to take part in the famous Russian Pasttime
        """
        
        # This function uses server-specific data
        server_dict = self.server_specific_data[message.channel.server]
        
        # Initialise Russian Roulette Values if not done yet
        if 'rr_bullet' not in server_dict:
            server_dict['rr_bullet'] = random.randint(1, 6)
            server_dict['rr_count'] = 1
        
        roulette_list = ['You spin the cylinder of the revolver with 1 bullet in it...',
                         '...you place the muzzle against your head and pull the trigger...',
                         '...your brain gets splattered all over the wall.',
                         '...you live to see another day.']

        # Send a blank message and edit it right away to make it show (edited) below the box
        # for properly aligning
        roulette_message = await self.safe_send_message(message.channel, "```%s\n\n ```" % (roulette_list[0]))
        await self.safe_edit_message(roulette_message, "```%s\n\n ```" % (roulette_list[0]))
        await asyncio.sleep(1)
        await self.safe_edit_message(roulette_message, "```%s\n%s\n ```" % (roulette_list[0], roulette_list[1]))
        await asyncio.sleep(2)
        
        # Splat!
        if server_dict['rr_bullet'] == server_dict['rr_count']:
            await self.safe_edit_message(roulette_message, "```%s\n%s\n%s```" % 
                (roulette_list[0], roulette_list[1], roulette_list[2]))
            
            # Save the number of shots fired before the death so the bot can recall it later
            temp_count = server_dict['rr_count']
            
            # Reload the gun
            server_dict['rr_bullet'] = random.randint(1, 6)
            server_dict['rr_count'] = 1
            await asyncio.sleep(1)
            return Response("%s died! The gun was fired %d times.\nKiyu reloaded the gun for you. <3" % 
                (message.author.mention, temp_count))
        # Death did not occur
        else:
            await self.safe_edit_message(roulette_message, "```%s\n%s\n%s```" % 
                (roulette_list[0], roulette_list[1], roulette_list[3]))
            server_dict['rr_count'] += 1
            await asyncio.sleep(1)
            return Response("Kiyu saw the gun get fired %d time%s so far." % 
                ((server_dict['rr_count'] - 1), 's' if (server_dict['rr_count'] - 1) > 1 else ''))
        
    async def cmd_say(self, message):
        """
        Usage:
            {command_prefix}say (message)

        Makes bot say something
        """
        
        # Delete the invoking message
        await self.safe_delete_message(message)
        
        say = message.content.replace(self.config.command_prefix + 'say', '').strip()
        return Response(say)  

    async def cmd_help(self, command=None):
        """
        Usage:
            {command_prefix}help [command]

        Prints a help message.
        If a command is specified, it prints a help message for that command.
        Otherwise, it lists the available commands.
        """

        if command:
            cmd = getattr(self, 'cmd_' + command, None)
            if cmd:
                return Response(
                    "```\n{}```".format(
                        dedent(cmd.__doc__),
                        command_prefix=self.config.command_prefix
                    ),
                    delete_after=60
                )
            else:
                return Response("No such command", delete_after=10)

        else:
            helpmsg = "Kiyu's commands are〜\n```"
            commands = []

            for att in dir(self):
                if att.startswith('cmd_') and att != 'cmd_help':
                    command_name = att.replace('cmd_', '').lower()
                    commands.append("{}{}".format(self.config.command_prefix, command_name))

            helpmsg += ", ".join(commands)
            helpmsg += "\n\n@me to talk to me!```"

            return Response(helpmsg, reply=True, delete_after=60)
         
    async def cmd_clean(self, message, channel, server, author, search_range=50):
        """
        Usage:
            {command_prefix}clean [range]

        Removes up to [range] messages the bot has posted in chat. Default: 50, Max: 1000
        """

        try:
            float(search_range)  # lazy check
            search_range = min(int(search_range), 1000)
        except:
            return Response("That's not what Kiyu expected. The format is `%sclean [optional range]`" 
                % (self.config.command_prefix))

        await self.safe_delete_message(message, quiet=True)

        def is_possible_command_invoke(entry):
            valid_call = any(
                entry.content.startswith(prefix) for prefix in [self.config.command_prefix])  # can be expanded
            return valid_call and not entry.content[1:2].isspace()

        delete_invokes = True
        delete_all = channel.permissions_for(author).manage_messages or self.config.owner_id == author.id

        def check(message):
            if is_possible_command_invoke(message) and delete_invokes:
                return delete_all or message.author == author
            return message.author == self.user

        if self.user.bot:
            if channel.permissions_for(server.me).manage_messages:
                deleted = await self.purge_from(channel, check=check, limit=search_range, before=message)
                return Response('Cleaned up {} message{}.'.format(len(deleted), 's' * bool(deleted)), delete_after=15)

        deleted = 0
        async for entry in self.logs_from(channel, search_range, before=message):
            if entry == self.server_specific_data[channel.server]['last_np_msg']:
                continue

            if entry.author == self.user:
                await self.safe_delete_message(entry)
                deleted += 1
                await asyncio.sleep(0.21)

            if is_possible_command_invoke(entry) and delete_invokes:
                if delete_all or entry.author == author:
                    try:
                        await self.delete_message(entry)
                        await asyncio.sleep(0.21)
                        deleted += 1

                    except discord.Forbidden:
                        delete_invokes = False
                    except discord.HTTPException:
                        pass

        return Response('Cleaned up {} message{}.'.format(deleted, 's' * bool(deleted)), delete_after=15)

    async def cmd_listids(self, server, author, leftover_args, cat='all'):
        """
        Usage:
            {command_prefix}listids [categories]
            
        Lists the ids for various things.  Categories are:
        all, users, roles, channels
        """

        cats = ['channels', 'roles', 'users']

        if cat not in cats and cat != 'all':
            return Response(
                "Valid categories: " + ' '.join(['`%s`' % c for c in cats]),
                reply=True,
                delete_after=25
            )

        if cat == 'all':
            requested_cats = cats
        else:
            requested_cats = [cat] + [c.strip(',') for c in leftover_args]

        data = ['Your ID: %s' % author.id]

        for cur_cat in requested_cats:
            rawudata = None

            if cur_cat == 'users':
                data.append("\nUser IDs:")
                rawudata = ['%s #%s: %s' % (m.name, m.discriminator, m.id) for m in server.members]

            elif cur_cat == 'roles':
                data.append("\nRole IDs:")
                rawudata = ['%s: %s' % (r.name, r.id) for r in server.roles]

            elif cur_cat == 'channels':
                data.append("\nText Channel IDs:")
                tchans = [c for c in server.channels if c.type == discord.ChannelType.text]
                rawudata = ['%s: %s' % (c.name, c.id) for c in tchans]

            if rawudata:
                data.extend(rawudata)

        with BytesIO() as sdata:
            sdata.writelines(d.encode('utf8') + b'\n' for d in data)
            sdata.seek(0)

            # TODO: Fix naming (Discord20API-ids.txt)
            await self.send_file(author, sdata, filename='%s-ids-%s.txt' % (server.name.replace(' ', '_'), cat))

        return Response(":mailbox_with_mail:", delete_after=20)    
        
    async def cmd_perms(self, author, channel, server, permissions):
        """
        Usage:
            {command_prefix}perms

        Sends the user a list of their permissions.
        """

        lines = ['Command permissions in %s\n' % server.name, '```', '```']

        for perm in permissions.__dict__:
            if perm in ['user_list'] or permissions.__dict__[perm] == set():
                continue

            lines.insert(len(lines) - 1, "%s: %s" % (perm, permissions.__dict__[perm]))

        await self.send_message(author, '\n'.join(lines))
        return Response(":mailbox_with_mail:", delete_after=20)
        
    @owner_only
    async def cmd_setname(self, leftover_args, name):
        """
        Usage:
            {command_prefix}setname name
            
        Changes the bot's username.
        Note: This operation is limited by discord to twice per hour.
        """

        name = ' '.join([name, *leftover_args])

        try:
            await self.edit_profile(username=name)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response(":ok_hand:", delete_after=20)

    @owner_only
    async def cmd_setnick(self, server, channel, leftover_args, nick):
        """
        Usage:
            {command_prefix}setnick nick
            
        Changes the bot's nickname.
        """

        if not channel.permissions_for(server.me).change_nicknames:
            raise exceptions.CommandError("Kiyu couldn't change nickname: no permission.")

        nick = ' '.join([nick, *leftover_args])

        try:
            await self.change_nickname(server.me, nick)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response(":ok_hand:", delete_after=20)

    @owner_only
    async def cmd_setavatar(self, message, url=None):
        """
        Usage:
            {command_prefix}setavatar [url]
            
        Changes the bot's avatar.
        Attaching a file and leaving the url parameter blank also works.
        """

        if message.attachments:
            thing = message.attachments[0]['url']
        else:
            thing = url.strip('<>')

        try:
            with aiohttp.Timeout(10):
                async with self.aiosession.get(thing) as res:
                    await self.edit_profile(avatar=await res.read())

        except Exception as e:
            raise exceptions.CommandError("Kiyu couldn't change avatar: %s" % e, expire_in=20)

        return Response(":ok_hand:", delete_after=20)

    async def cmd_game(self, message):
        """
        Usage:
            {command_prefix}setgame (message)

        Makes bot play a game
        Bot removes its game if there is no message specified
        """
        game_name = message.content.replace(self.config.command_prefix + 'game', '').strip()
        
        if len(game_name) > 0:
            game = discord.Game(name=game_name)
            await self.change_status(game=game)
            return Response("Kiyu is now playing %s." % (game_name)) 
        else:
            await self.change_status(game=None)
            return Response("Kiyu is no longer playing.")     
    
    @owner_only    
    async def cmd_restart(self, channel):
        await self.safe_send_message(channel, ":wave:")
        raise exceptions.RestartSignal

    @owner_only
    async def cmd_shutdown(self, channel):
        await self.safe_send_message(channel, ":wave:")
        raise exceptions.TerminateSignal
        
    async def on_message(self, message):
        await self.wait_until_ready()
        
        # Do not reply to bot messages
        if message.author.bot:
            return

        message_content = message.content.strip()
        
        # Message sent was an invalid command/not a command
        if not message_content.startswith(self.config.command_prefix):
            # Cleverbot Interaction when bot
            if message_content.find(self.user.mention) != -1:
                string_sent = message_content.replace(self.user.mention, '').strip()
                string_reply = message.author.mention + ' ' + self.cleverwrap.say(string_sent)
                
                sentmsg = await self.safe_send_message(
                        message.channel, string_reply
                    )
            return

        if message.author == self.user:
            self.safe_print("Ignoring command from Kiyu (%s)" % message.content)
            return

        if self.config.bound_channels and message.channel.id not in self.config.bound_channels and not message.channel.is_private:
            return

        command, *args = message_content.split()
        command = command[len(self.config.command_prefix):].lower().strip()

        handler = getattr(self, 'cmd_%s' % command, None)
        
        # Invalid Command
        if not handler:
            return

        if message.channel.is_private:
            if not (message.author.id == self.config.owner_id and command == 'joinserver'):
                await self.send_message(message.channel, "Kiyu doesn't reply to private messages.")
                return

        self.safe_print("[Command] {0.id}/{0.name} ({1})".format(message.author, message_content))

        user_permissions = self.permissions.for_user(message.author)

        argspec = inspect.signature(handler)
        params = argspec.parameters.copy()

        # noinspection PyBroadException
        try:

            handler_kwargs = {}
            if params.pop('message', None):
                handler_kwargs['message'] = message

            if params.pop('channel', None):
                handler_kwargs['channel'] = message.channel

            if params.pop('author', None):
                handler_kwargs['author'] = message.author

            if params.pop('server', None):
                handler_kwargs['server'] = message.server

            if params.pop('player', None):
                handler_kwargs['player'] = await self.get_player(message.channel)

            if params.pop('permissions', None):
                handler_kwargs['permissions'] = user_permissions

            if params.pop('user_mentions', None):
                handler_kwargs['user_mentions'] = list(map(message.server.get_member, message.raw_mentions))

            if params.pop('channel_mentions', None):
                handler_kwargs['channel_mentions'] = list(map(message.server.get_channel, message.raw_channel_mentions))

            if params.pop('leftover_args', None):
                handler_kwargs['leftover_args'] = args

            args_expected = []
            for key, param in list(params.items()):
                doc_key = '[%s=%s]' % (key, param.default) if param.default is not inspect.Parameter.empty else key
                args_expected.append(doc_key)

                if not args and param.default is not inspect.Parameter.empty:
                    params.pop(key)
                    continue

                if args:
                    arg_value = args.pop(0)
                    handler_kwargs[key] = arg_value
                    params.pop(key)

            if message.author.id != self.config.owner_id:
                if user_permissions.command_whitelist and command not in user_permissions.command_whitelist:
                    raise exceptions.PermissionsError(
                        "This command is not enabled for your group (%s)." % user_permissions.name,
                        expire_in=20)

                elif user_permissions.command_blacklist and command in user_permissions.command_blacklist:
                    raise exceptions.PermissionsError(
                        "This command is disabled for your group (%s)." % user_permissions.name,
                        expire_in=20)

            if params:
                docs = getattr(handler, '__doc__', None)
                if not docs:
                    docs = 'Usage: {}{} {}'.format(
                        self.config.command_prefix,
                        command,
                        ' '.join(args_expected)
                    )

                docs = '\n'.join(l.strip() for l in docs.split('\n'))
                await self.safe_send_message(
                    message.channel,
                    '```\n%s\n```' % docs.format(command_prefix=self.config.command_prefix),
                    expire_in=60
                )
                return

            response = await handler(**handler_kwargs)
            if response and isinstance(response, Response):
                content = response.content
                if response.reply:
                    content = '%s, %s' % (message.author.mention, content)

                sentmsg = await self.safe_send_message(
                    message.channel, content,
                    expire_in=response.delete_after if self.config.delete_messages else 0,
                    also_delete=message if self.config.delete_invoking else None
                )

        except (exceptions.CommandError, exceptions.HelpfulError, exceptions.ExtractionError) as e:
            print("{0.__class__}: {0.message}".format(e))

            expirein = e.expire_in if self.config.delete_messages else None
            alsodelete = message if self.config.delete_invoking else None

            await self.safe_send_message(
                message.channel,
                '```\n%s\n```' % e.message,
                expire_in=expirein,
                also_delete=alsodelete
            )

        except exceptions.Signal:
            raise

        except Exception:
            traceback.print_exc()
            if self.config.debug_mode:
                await self.safe_send_message(message.channel, '```\n%s\n```' % traceback.format_exc())

    async def on_server_update(self, before:discord.Server, after:discord.Server):
        if before.region != after.region:
            self.safe_print("[Servers] \"%s\" changed regions: %s -> %s" % (after.name, before.region, after.region))


if __name__ == '__main__':
    bot = KiyuBot()
    bot.run()
