"""Solcast API."""
from __future__ import annotations

import asyncio
import aiofiles
import copy
import json
import logging
import math
import os
import sys
import time
import traceback
import random
import re
from .spline import cubic_interp
from dataclasses import dataclass
from datetime import datetime as dt
from datetime import timedelta, timezone
from operator import itemgetter
from os.path import exists as file_exists
from typing import Any, Dict, cast

import async_timeout
from aiohttp import ClientConnectionError, ClientSession
from aiohttp.client_reqrep import ClientResponse
from isodate import parse_datetime

# for current func name, specify 0 or no argument.
# for name of caller of current func, specify 1.
# for name of caller of caller of current func, specify 2. etc.
currentFuncName = lambda n=0: sys._getframe(n + 1).f_code.co_name

_SENSOR_DEBUG_LOGGING = False

_JSON_VERSION = 4
_LOGGER = logging.getLogger(__name__)

class DateTimeEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, dt):
            return o.isoformat()

class JSONDecoder(json.JSONDecoder):
    def __init__(self, *args, **kwargs):
        json.JSONDecoder.__init__(
            self, object_hook=self.object_hook, *args, **kwargs)

    def object_hook(self, obj):
        ret = {}
        for key, value in obj.items():
            if key in {'period_start'}:
                ret[key] = dt.fromisoformat(value)
            else:
                ret[key] = value
        return ret

statusTranslate = {
    200: 'Success',
    401: 'Unauthorized',
    403: 'Forbidden',
    404: 'Not found',
    418: 'I\'m a teapot', # Included here for fun. An April Fools joke in 1998. Included in RFC2324#section-2.3.2
    429: 'Solcast too busy',
    500: 'Internal web server error',
    501: 'Not implemented',
    502: 'Bad gateway',
    503: 'Service unavailable',
    504: 'Gateway timeout',
}

def translate(status):
    return ('%s/%s' % (str(status), statusTranslate[status], )) if statusTranslate.get(status) else status


@dataclass
class ConnectionOptions:
    """Solcast API options for connection."""

    api_key: str
    host: str
    file_path: str
    tz: timezone
    dampening: dict
    customhoursensor: int
    key_estimate: str
    hard_limit: int
    attr_brk_estimate: bool
    attr_brk_estimate10: bool
    attr_brk_estimate90: bool
    attr_brk_site: bool
    attr_brk_halfhourly: bool
    attr_brk_hourly: bool


class SolcastApi:
    """Solcast API"""

    def __init__(
        self,
        aiohttp_session: ClientSession,
        options: ConnectionOptions,
        apiCacheEnabled: bool = False
    ):
        """Device init."""
        self.aiohttp_session = aiohttp_session
        self.options = options
        self.apiCacheEnabled = apiCacheEnabled
        self._sites = []
        self._data = {'siteinfo': {}, 'last_updated': dt.fromtimestamp(0, timezone.utc).isoformat()}
        self._tally = {}
        self._api_used = {}
        self._api_limit = {}
        self._filename = options.file_path
        self._tz = options.tz
        self._dataenergy = {}
        self._data_forecasts = []
        self._site_data_forecasts = {}
        self._forecasts_start_idx = 0
        self._detailedForecasts = []
        self._loaded_data = False
        self._serialize_lock = asyncio.Lock()
        self._damp =options.dampening
        self._customhoursensor = options.customhoursensor
        self._use_data_field = f"pv_{options.key_estimate}"
        self._hardlimit = options.hard_limit
        self._estimen = {'pv_estimate': options.attr_brk_estimate, 'pv_estimate10': options.attr_brk_estimate10, 'pv_estimate90': options.attr_brk_estimate90}
        #self._weather = ""

    async def serialize_data(self):
        """Serialize data to file."""
        try:
            if not self._loaded_data:
                _LOGGER.debug("Not saving forecast cache in serialize_data() as no data has been loaded yet")
                return
            # If the _loaded_data flag is True, yet last_updated is 1/1/1970 then data has not been loaded
            # properly for some reason, or no forecast has been received since start.
            # Abort the save.
            if self._data['last_updated'] == dt.fromtimestamp(0, timezone.utc).isoformat():
                _LOGGER.error("Internal error: Solcast forecast cache date has not been set, not saving data")
                return

            async with self._serialize_lock:
                async with aiofiles.open(self._filename, "w") as f:
                    await f.write(json.dumps(self._data, ensure_ascii=False, cls=DateTimeEncoder))
                    _LOGGER.debug("Saved forecast cache")
        except Exception as ex:
            _LOGGER.error("Exception in serialize_data(): %s", ex)
            _LOGGER.error(traceback.format_exc())

    def redact_api_key(self, api_key):
        return '*'*6 + api_key[-6:]

    def redact_msg_api_key(self, msg, api_key):
        return msg.replace(api_key, self.redact_api_key(api_key))

    async def write_api_usage_cache_file(self, json_file, json_content, api_key):
        try:
            _LOGGER.debug(f"Writing API usage cache file: {self.redact_msg_api_key(json_file, api_key)}")
            async with aiofiles.open(json_file, 'w') as f:
                await f.write(json.dumps(json_content, ensure_ascii=False))
        except Exception as ex:
            _LOGGER.error("Exception in write_api_usage_cache_file(): %s", ex)
            _LOGGER.error(traceback.format_exc())

    def get_api_usage_cache_filename(self, entry_name):
        return "/config/solcast-usage%s.json" % ("" if len(self.options.api_key.split(",")) < 2 else "-" + entry_name) # For more than one API key use separate files

    def get_api_sites_cache_filename(self, entry_name):
        return "/config/solcast-sites%s.json" % ("" if len(self.options.api_key.split(",")) < 2 else "-" + entry_name) # Ditto

    async def reset_api_usage(self):
        for api_key in self._api_used.keys():
            self._api_used[api_key] = 0
            await self.write_api_usage_cache_file(
                self.get_api_usage_cache_filename(api_key),
                {"daily_limit": self._api_limit[api_key], "daily_limit_consumed": self._api_used[api_key]},
                api_key
            )

    async def sites_data(self):
        """Request data via the Solcast API."""

        try:
            def redact(s):
                return re.sub(r'itude\': [0-9\-\.]+', 'itude\': **.******', s)
            sp = self.options.api_key.split(",")
            for spl in sp:
                params = {"format": "json", "api_key": spl.strip()}
                async with async_timeout.timeout(60):
                    apiCacheFileName = self.get_api_sites_cache_filename(spl)
                    _LOGGER.debug(f"{'Sites cache ' + ('exists' if file_exists(apiCacheFileName) else 'does not yet exist')}")
                    if self.apiCacheEnabled and file_exists(apiCacheFileName):
                        _LOGGER.debug(f"Loading cached sites data")
                        status = 404
                        async with aiofiles.open(apiCacheFileName) as f:
                            resp_json = json.loads(await f.read())
                            status = 200
                    else:
                        _LOGGER.debug(f"Connecting to {self.options.host}/rooftop_sites?format=json&api_key={self.redact_api_key(spl)}")
                        retries = 3
                        retry = retries
                        success = False
                        useCacheImmediate = False
                        cacheExists = file_exists(apiCacheFileName)
                        while retry >= 0:
                            resp: ClientResponse = await self.aiohttp_session.get(
                                url=f"{self.options.host}/rooftop_sites", params=params, ssl=False
                            )

                            status = resp.status
                            _LOGGER.debug(f"HTTP session returned status {translate(status)} in sites_data()")
                            try:
                                resp_json = await resp.json(content_type=None)
                            except json.decoder.JSONDecodeError:
                                _LOGGER.error("JSONDecodeError in sites_data(): Solcast site could be having problems")
                            except: raise

                            if status == 200:
                                _LOGGER.debug(f"Writing sites cache")
                                async with aiofiles.open(apiCacheFileName, 'w') as f:
                                    await f.write(json.dumps(resp_json, ensure_ascii=False))
                                success = True
                                break
                            else:
                                if cacheExists:
                                    useCacheImmediate = True
                                    break
                                if retry > 0:
                                    _LOGGER.debug(f"Will retry get sites, retry {(retries - retry) + 1}")
                                    await asyncio.sleep(5)
                                retry -= 1
                        if not success:
                            if not useCacheImmediate:
                                _LOGGER.warning(f"Retries exhausted gathering Solcast sites, last call result: {translate(status)}, using cached data if it exists")
                            status = 404
                            if cacheExists:
                                async with aiofiles.open(apiCacheFileName) as f:
                                    resp_json = json.loads(await f.read())
                                    status = 200
                                _LOGGER.info(f"Loaded sites cache for {self.redact_api_key(spl)}")
                            else:
                                _LOGGER.error(f"Cached Solcast sites are not yet available for {self.redact_api_key(spl)} to cope with API call failure")
                                _LOGGER.error(f"At least one successful API 'get sites' call is needed, so the integration will not function correctly")

                if status == 200:
                    d = cast(dict, resp_json)
                    _LOGGER.debug(f"Sites data: {redact(str(d))}")
                    for i in d['sites']:
                        i['apikey'] = spl.strip()
                        #v4.0.14 to stop HA adding a pin to the map
                        i.pop('longitude', None)
                        i.pop('latitude', None)
                    self._sites = self._sites + d['sites']
                else:
                    _LOGGER.error(f"{self.options.host} HTTP status error {translate(status)} in sites_data() while gathering sites")
                    _LOGGER.error(f"Solcast integration did not start correctly, as site(s) are needed. Suggestion: Restart the integration")
                    raise Exception(f"HTTP sites_data error: Solcast Error gathering sites")
        except ConnectionRefusedError as err:
            _LOGGER.error("Connection refused in sites_data(): %s", err)
        except ClientConnectionError as e:
            _LOGGER.error('Connection error in sites_data(): %s', str(e))
        except asyncio.TimeoutError:
            try:
                _LOGGER.warning("Retrieving Solcast sites timed out, attempting to continue")
                error = False
                for spl in sp:
                    apiCacheFileName = self.get_api_sites_cache_filename(spl)
                    cacheExists = file_exists(apiCacheFileName)
                    if cacheExists:
                        _LOGGER.info("Loading cached Solcast sites for {self.redact_api_key(spl)}")
                        async with aiofiles.open(apiCacheFileName) as f:
                            resp_json = json.loads(await f.read())
                            d = cast(dict, resp_json)
                            _LOGGER.debug(f"Sites data: {redact(str(d))}")
                            for i in d['sites']:
                                i['apikey'] = spl.strip()
                                #v4.0.14 to stop HA adding a pin to the map
                                i.pop('longitude', None)
                                i.pop('latitude', None)
                            self._sites = self._sites + d['sites']
                            _LOGGER.info(f"Loaded sites cache for {self.redact_api_key(spl)}")
                    else:
                        error = True
                        _LOGGER.error(f"Cached sites are not yet available for {self.redact_api_key(spl)} to cope with Solcast API call failure")
                        _LOGGER.error(f"At least one successful API 'get sites' call is needed, so the integration cannot function")
                if error:
                    _LOGGER.error("Timed out getting Solcast sites, and one or more site caches failed to load")
                    _LOGGER.error("This is critical, and the integration cannot function reliably")
                    _LOGGER.error("Suggestion: Double check your overall HA configuration, specifically networking related")
            except Exception as e:
                pass
        except Exception as e:
            _LOGGER.error("Exception in sites_data(): %s", traceback.format_exc())

    async def sites_usage(self):
        """Request api usage via the Solcast API."""

        try:
            sp = self.options.api_key.split(",")

            for spl in sp:
                sitekey = spl.strip()
                #params = {"format": "json", "api_key": self.options.api_key}
                params = {"api_key": sitekey}
                _LOGGER.debug(f"Getting API limit and usage from solcast for {self.redact_api_key(sitekey)}")
                async with async_timeout.timeout(60):
                    apiCacheFileName = self.get_api_usage_cache_filename(sitekey)
                    _LOGGER.debug(f"{'API usage cache ' + ('exists' if file_exists(apiCacheFileName) else 'does not yet exist')}")
                    retries = 3
                    retry = retries
                    success = False
                    useCacheImmediate = False
                    cacheExists = file_exists(apiCacheFileName)
                    while retry > 0:
                        resp: ClientResponse = await self.aiohttp_session.get(
                            url=f"{self.options.host}/json/reply/GetUserUsageAllowance", params=params, ssl=False
                        )
                        status = resp.status
                        try:
                            resp_json = await resp.json(content_type=None)
                        except json.decoder.JSONDecodeError:
                            _LOGGER.error("JSONDecodeError in sites_usage() - Solcast site could be having problems")
                        except: raise
                        _LOGGER.debug(f"HTTP session returned status {translate(status)} in sites_usage()")
                        if status == 200:
                            await self.write_api_usage_cache_file(apiCacheFileName, resp_json, sitekey)
                            retry = 0
                            success = True
                        else:
                            if cacheExists:
                                useCacheImmediate = True
                                break
                            _LOGGER.debug(f"Will retry GetUserUsageAllowance, retry {(retries - retry) + 1}")
                            await asyncio.sleep(5)
                            retry -= 1
                    if not success:
                        if not useCacheImmediate:
                            _LOGGER.warning(f"Timeout getting Solcast API usage allowance, last call result: {translate(status)}, using cached data if it exists")
                        status = 404
                        if cacheExists:
                            async with aiofiles.open(apiCacheFileName) as f:
                                resp_json = json.loads(await f.read())
                                status = 200
                            _LOGGER.info(f"Loaded API usage cache")
                        else:
                            _LOGGER.warning(f"No Solcast API usage cache found")

                if status == 200:
                    d = cast(dict, resp_json)
                    self._api_limit[sitekey] = d.get("daily_limit", None)
                    self._api_used[sitekey] = d.get("daily_limit_consumed", None)
                    _LOGGER.debug(f"API counter for {self.redact_api_key(sitekey)} is {self._api_used[sitekey]}/{self._api_limit[sitekey]}")
                else:
                    self._api_limit[sitekey] = 10
                    self._api_used[sitekey] = 0
                    raise Exception(f"Gathering site usage failed in sites_usage(). Request returned Status code: {translate(status)} - Response: {resp_json}.")

        except json.decoder.JSONDecodeError:
            _LOGGER.error("JSONDecodeError in sites_usage(): Solcast site could be having problems")
        except ConnectionRefusedError as err:
            _LOGGER.error("Error in sites_usage(): %s", err)
        except ClientConnectionError as e:
            _LOGGER.error('Connection error in sites_usage(): %s', str(e))
        except asyncio.TimeoutError:
            _LOGGER.error("Connection error in sites_usage(): Timed out connecting to solcast server")
        except Exception as e:
            _LOGGER.error("Exception in sites_usage(): %s", traceback.format_exc())

    # async def sites_weather(self):
    #     """Request site weather byline via the Solcast API."""

    #     try:
    #         if len(self._sites) > 0:
    #             sp = self.options.api_key.split(",")
    #             rid = self._sites[0].get("resource_id", None)

    #             params = {"resourceId": rid, "api_key": sp[0]}
    #             _LOGGER.debug(f"Get weather byline from solcast")
    #             async with async_timeout.timeout(60):
    #                 resp: ClientResponse = await self.aiohttp_session.get(
    #                     url=f"https://api.solcast.com.au/json/reply/GetRooftopSiteSparklines", params=params, ssl=False
    #                 )
    #                 resp_json = await resp.json(content_type=None)
    #                 status = resp.status

    #             if status == 200:
    #                 d = cast(dict, resp_json)
    #                 _LOGGER.debug(f"Returned data in sites_weather(): {d}")
    #                 self._weather = d.get("forecast_descriptor", None).get("description", None)
    #                 _LOGGER.debug(f"Weather description: {self._weather}")
    #             else:
    #                 raise Exception(f"Gathering weather description failed. request returned Status code: {translate(status)} - Response: {resp_json}.")

    #     except json.decoder.JSONDecodeError:
    #         _LOGGER.error("JSONDecodeError in sites_weather(): Solcast site could be having problems")
    #     except ConnectionRefusedError as err:
    #         _LOGGER.error("Error in sites_weather(): %s", err)
    #     except ClientConnectionError as e:
    #         _LOGGER.error("Connection error in sites_weather(): %s", str(e))
    #     except asyncio.TimeoutError:
    #         _LOGGER.error("Connection Error in sites_weather(): Timed out connection to solcast server")
    #     except Exception as e:
    #         _LOGGER.error("Error in sites_weather(): %s", traceback.format_exc())

    async def load_saved_data(self):
        try:
            if len(self._sites) > 0:
                if file_exists(self._filename):
                    async with aiofiles.open(self._filename) as data_file:
                        jsonData = json.loads(await data_file.read(), cls=JSONDecoder)
                        json_version = jsonData.get("version", 1)
                        #self._weather = jsonData.get("weather", "unknown")
                        _LOGGER.debug(f"The saved data file exists, file type is {type(jsonData)}")
                        if json_version == _JSON_VERSION:
                            self._data = jsonData
                            self._loaded_data = True

                            #any new API keys so no sites data yet for those
                            ks = {}
                            for d in self._sites:
                                if not any(s == d.get('resource_id', '') for s in jsonData['siteinfo']):
                                    ks[d.get('resource_id')] = d.get('apikey')

                            if len(ks.keys()) > 0:
                                #some site data does not exist yet so go and get it
                                _LOGGER.debug("Likely a new API key added, getting the data for it")
                                for a in ks:
                                    await self.http_data_call(self.get_api_usage_cache_filename(ks[a]), r_id=a, api=ks[a], dopast=True)
                                await self.serialize_data()

                            #any site changes that need to be removed
                            l = []
                            for s in jsonData['siteinfo']:
                                if not any(d.get('resource_id', '') == s for d in self._sites):
                                    _LOGGER.info(f"Solcast site resource id {s} no longer part of your system, removing saved data from cached file")
                                    l.append(s)

                            for ll in l:
                                del jsonData['siteinfo'][ll]

                            #create an up to date forecast and make sure the TZ fits just in case its changed
                            await self.buildforecastdata()
                            _LOGGER.info(f"Loaded solcast.json forecast cache")

                if not self._loaded_data:
                    #no file to load
                    _LOGGER.warning(f"There is no solcast.json to load, so fetching solar forecast, including past forecasts")
                    #could be a brand new install of the integation so this is poll once now automatically
                    await self.http_data(dopast=True)

                if self._loaded_data: return True
            else:
                _LOGGER.error(f"Solcast site count is zero in load_saved_data(); the get sites must have failed, and there is no sites cache")
                return True # Not really successful, but don't want the retry in __init__
        except json.decoder.JSONDecodeError:
            _LOGGER.error("The cached data in solcast.json is corrupt in load_saved_data()")
        except Exception as e:
            _LOGGER.error("Exception in load_saved_data(): %s", traceback.format_exc())
        return False

    async def delete_solcast_file(self, *args):
        _LOGGER.debug(f"Service event to delete old solcast.json file")
        try:
            if file_exists(self._filename):
                os.remove(self._filename)
                await self.sites_data()
                await self.sites_usage()
                await self.load_saved_data()
            else:
                _LOGGER.warning("There is no solcast.json to delete")
        except Exception:
            _LOGGER.error(f"Service event to delete old solcast.json file failed")

    async def get_forecast_list(self, *args):
        try:
            st_time = time.time()

            st_i, end_i = self.get_forecast_list_slice(self._data_forecasts, args[0], args[1], search_past=True)
            h = self._data_forecasts[st_i:end_i]

            if _SENSOR_DEBUG_LOGGING: _LOGGER.debug("Get forecast list: (%ss) st %s end %s st_i %d end_i %d h.len %d",
                            round(time.time()-st_time,4), args[0], args[1], st_i, end_i, len(h))

            return tuple(
                    {**d, "period_start": d["period_start"].astimezone(self._tz)} for d in h
                )

        except Exception:
            _LOGGER.error(f"Service event to get list of Solcast forecasts failed")
            return None

    def get_api_used_count(self):
        """Return API polling count for this UTC 24hr period"""
        used = 0
        for k, v in self._api_used.items(): used += v
        return used

    def get_api_limit(self):
        """Return API polling limit for this account"""
        try:
            limit = 0
            for k, v in self._api_limit.items(): limit += v
            return limit
        except Exception:
            return None

    # def get_weather(self):
    #     """Return weather description"""
    #     return self._weather

    def get_last_updated_datetime(self) -> dt:
        """Return date time with the data was last updated"""
        return dt.fromisoformat(self._data["last_updated"])

    def get_rooftop_site_total_today(self, site) -> float:
        """Return a site total kW for today"""
        if self._tally.get(site) == None: _LOGGER.warning(f"Site total kW forecast today is currently unavailable for {site}")
        return self._tally.get(site)

    def get_rooftop_site_extra_data(self, site = ""):
        """Return a site information"""
        g = tuple(d for d in self._sites if d["resource_id"] == site)
        if len(g) != 1:
            raise ValueError(f"Unable to find site {site}")
        site: Dict[str, Any] = g[0]
        ret = {}

        ret["name"] = site.get("name", None)
        ret["resource_id"] = site.get("resource_id", None)
        ret["capacity"] = site.get("capacity", None)
        ret["capacity_dc"] = site.get("capacity_dc", None)
        ret["longitude"] = site.get("longitude", None)
        ret["latitude"] = site.get("latitude", None)
        ret["azimuth"] = site.get("azimuth", None)
        ret["tilt"] = site.get("tilt", None)
        ret["install_date"] = site.get("install_date", None)
        ret["loss_factor"] = site.get("loss_factor", None)
        for key in tuple(ret.keys()):
            if ret[key] is None:
                ret.pop(key, None)

        return ret

    def get_now_utc(self):
        return dt.now(self._tz).astimezone(timezone.utc)

    def get_hour_start_utc(self):
        return dt.now(self._tz).replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    def get_day_start_utc(self):
        return dt.now(self._tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    def get_forecast_day(self, futureday) -> Dict[str, Any]:
        """Return Solcast Forecasts data for the Nth day ahead"""
        noDataError = True

        start_utc = self.get_day_start_utc() + timedelta(days=futureday)
        end_utc = start_utc + timedelta(days=1)
        st_i, end_i = self.get_forecast_list_slice(self._data_forecasts, start_utc, end_utc)
        h = self._data_forecasts[st_i:end_i]

        if _SENSOR_DEBUG_LOGGING: _LOGGER.debug("Get forecast day: %d st %s end %s st_i %d end_i %d h.len %d",
                        futureday,
                        start_utc.strftime('%Y-%m-%d %H:%M:%S'),
                        end_utc.strftime('%Y-%m-%d %H:%M:%S'),
                        st_i, end_i, len(h))

        tup = tuple(
                {**d, "period_start": d["period_start"].astimezone(self._tz)} for d in h
            )

        if len(tup) < 48:
            noDataError = False

        hourlyturp = []
        for index in range(0,len(tup),2):
            if len(tup)>0:
                try:
                    x1 = round((tup[index]["pv_estimate"] + tup[index+1]["pv_estimate"]) /2, 4)
                    x2 = round((tup[index]["pv_estimate10"] + tup[index+1]["pv_estimate10"]) /2, 4)
                    x3 = round((tup[index]["pv_estimate90"] + tup[index+1]["pv_estimate90"]) /2, 4)
                    hourlyturp.append({"period_start":tup[index]["period_start"], "pv_estimate":x1, "pv_estimate10":x2, "pv_estimate90":x3})
                except IndexError:
                    x1 = round((tup[index]["pv_estimate"]), 4)
                    x2 = round((tup[index]["pv_estimate10"]), 4)
                    x3 = round((tup[index]["pv_estimate90"]), 4)
                    hourlyturp.append({"period_start":tup[index]["period_start"], "pv_estimate":x1, "pv_estimate10":x2, "pv_estimate90":x3})
                except Exception as ex:
                    _LOGGER.error("Exception in get_forecast_day(): %s", ex)
                    _LOGGER.error(traceback.format_exc())

        res = {
            "dayname": start_utc.astimezone(self._tz).strftime("%A"),
            "dataCorrect": noDataError,
        }
        if self.options.attr_brk_halfhourly: res["detailedForecast"] = tup
        if self.options.attr_brk_hourly: res["detailedHourly"] = hourlyturp
        return res

    def get_forecast_n_hour(self, n_hour, site=None, _use_data_field=None) -> int:
        """Return Solcast Forecast for the Nth hour"""
        start_utc = self.get_hour_start_utc() + timedelta(hours=n_hour)
        end_utc = start_utc + timedelta(hours=1)
        res = round(500 * self.get_forecast_pv_estimates(start_utc, end_utc, site=site, _use_data_field=_use_data_field))
        return res

    def get_forecasts_n_hour(self, n_hour) -> Dict[str, Any]:
        res = {}
        if self.options.attr_brk_site:
            for site in self._sites:
                res[site['resource_id']] = self.get_forecast_n_hour(n_hour, site=site['resource_id'])
                for _data_field in ('pv_estimate', 'pv_estimate10', 'pv_estimate90'):
                    if self._estimen.get(_data_field): res[_data_field.replace('pv_','')+'-'+site['resource_id']] = self.get_forecast_n_hour(n_hour, site=site['resource_id'], _use_data_field=_data_field)
        for _data_field in ('pv_estimate', 'pv_estimate10', 'pv_estimate90'):
            if self._estimen.get(_data_field): res[_data_field.replace('pv_','')] = self.get_forecast_n_hour(n_hour, _use_data_field=_data_field)
        return res

    def get_forecast_custom_hours(self, n_hours, site=None, _use_data_field=None) -> int:
        """Return Solcast Forecast for the next N hours"""
        start_utc = self.get_now_utc()
        end_utc = start_utc + timedelta(hours=n_hours)
        res = round(500 * self.get_forecast_pv_estimates(start_utc, end_utc, site=site, _use_data_field=_use_data_field))
        return res

    def get_forecasts_custom_hours(self, n_hour) -> Dict[str, Any]:
        res = {}
        if self.options.attr_brk_site:
            for site in self._sites:
                res[site['resource_id']] = self.get_forecast_custom_hours(n_hour, site=site['resource_id'])
                for _data_field in ('pv_estimate', 'pv_estimate10', 'pv_estimate90'):
                    if self._estimen.get(_data_field): res[_data_field.replace('pv_','')+'-'+site['resource_id']] = self.get_forecast_custom_hours(n_hour, site=site['resource_id'], _use_data_field=_data_field)
        for _data_field in ('pv_estimate', 'pv_estimate10', 'pv_estimate90'):
            if self._estimen.get(_data_field): res[_data_field.replace('pv_','')] = self.get_forecast_custom_hours(n_hour, _use_data_field=_data_field)
        return res

    def get_power_n_mins(self, n_mins, site=None, _use_data_field=None) -> int:
        """Return Solcast Power for the next N minutes"""
        # uses a rolling 20mins interval (arbitrary decision) to smooth out the transitions between the 30mins intervals
        start_utc = self.get_now_utc() + timedelta(minutes=n_mins-10)
        end_utc = start_utc + timedelta(minutes=20)
        # multiply with 1.5 as the power reported is only for a 20mins interval (out of 30mins)
        res = round(1000 * 1.5 * self.get_forecast_pv_estimates(start_utc, end_utc, site=site, _use_data_field=_use_data_field))
        return res

    def get_sites_power_n_mins(self, n_mins) -> Dict[str, Any]:
        res = {}
        if self.options.attr_brk_site:
            for site in self._sites:
                res[site['resource_id']] = self.get_power_n_mins(n_mins, site=site['resource_id'])
                for _data_field in ('pv_estimate', 'pv_estimate10', 'pv_estimate90'):
                    if self._estimen.get(_data_field): res[_data_field.replace('pv_','')+'-'+site['resource_id']] = self.get_power_n_mins(n_mins, site=site['resource_id'], _use_data_field=_data_field)
        for _data_field in ('pv_estimate', 'pv_estimate10', 'pv_estimate90'):
            if self._estimen.get(_data_field): res[_data_field.replace('pv_','')] = self.get_power_n_mins(n_mins, site=None, _use_data_field=_data_field)
        return res

    def get_peak_w_day(self, n_day, site=None, _use_data_field=None) -> int:
        """Return max kW for site N days ahead"""
        _data_field = self._use_data_field if _use_data_field is None else _use_data_field
        start_utc = self.get_day_start_utc() + timedelta(days=n_day)
        end_utc = start_utc + timedelta(days=1)
        res = self.get_max_forecast_pv_estimate(start_utc, end_utc, site=site, _use_data_field=_data_field)
        return 0 if res is None else round(1000 * res[_data_field])

    def get_sites_peak_w_day(self, n_day) -> Dict[str, Any]:
        res = {}
        if self.options.attr_brk_site:
            for site in self._sites:
                res[site['resource_id']] = self.get_peak_w_day(n_day, site=site['resource_id'])
                for _data_field in ('pv_estimate', 'pv_estimate10', 'pv_estimate90'):
                    if self._estimen.get(_data_field): res[_data_field.replace('pv_','')+'-'+site['resource_id']] = self.get_peak_w_day(n_day, site=site['resource_id'], _use_data_field=_data_field)
        for _data_field in ('pv_estimate', 'pv_estimate10', 'pv_estimate90'):
            if self._estimen.get(_data_field): res[_data_field.replace('pv_','')] = self.get_peak_w_day(n_day, site=None, _use_data_field=_data_field)
        return res

    def get_peak_w_time_day(self, n_day, site=None, _use_data_field=None) -> dt:
        """Return hour of max kW for site N days ahead"""
        start_utc = self.get_day_start_utc() + timedelta(days=n_day)
        end_utc = start_utc + timedelta(days=1)
        res = self.get_max_forecast_pv_estimate(start_utc, end_utc, site=site, _use_data_field=_use_data_field)
        return res if res is None else res["period_start"]

    def get_sites_peak_w_time_day(self, n_day) -> Dict[str, Any]:
        res = {}
        if self.options.attr_brk_site:
            for site in self._sites:
                res[site['resource_id']] = self.get_peak_w_time_day(n_day, site=site['resource_id'])
                for _data_field in ('pv_estimate', 'pv_estimate10', 'pv_estimate90'):
                    if self._estimen.get(_data_field): res[_data_field.replace('pv_','')+'-'+site['resource_id']] = self.get_peak_w_time_day(n_day, site=site['resource_id'], _use_data_field=_data_field)
        for _data_field in ('pv_estimate', 'pv_estimate10', 'pv_estimate90'):
            if self._estimen.get(_data_field): res[_data_field.replace('pv_','')] = self.get_peak_w_time_day(n_day, site=None, _use_data_field=_data_field)
        return res

    def get_forecast_remaining_today(self, site=None, _use_data_field=None) -> float:
        """Return remaining forecasted production for today"""
        # time remaining today
        start_utc = self.get_now_utc()
        end_utc = self.get_day_start_utc() + timedelta(days=1)
        res = round(0.5 * self.get_forecast_pv_estimates(start_utc, end_utc, site=site, _use_data_field=_use_data_field, interpolate=True), 4)
        return res

    def get_forecasts_remaining_today(self) -> Dict[str, Any]:
        res = {}
        if self.options.attr_brk_site:
            for site in self._sites:
                res[site['resource_id']] = self.get_forecast_remaining_today(site=site['resource_id'])
                for _data_field in ('pv_estimate', 'pv_estimate10', 'pv_estimate90'):
                    if self._estimen.get(_data_field): res[_data_field.replace('pv_','')+'-'+site['resource_id']] = self.get_forecast_remaining_today(site=site['resource_id'], _use_data_field=_data_field)
        for _data_field in ('pv_estimate', 'pv_estimate10', 'pv_estimate90'):
            if self._estimen.get(_data_field): res[_data_field.replace('pv_','')] = self.get_forecast_remaining_today(_use_data_field=_data_field)
        return res

    def get_total_kwh_forecast_day(self, n_day, site=None, _use_data_field=None) -> float:
        """Return forecast kWh total for site N days ahead"""
        start_utc = self.get_day_start_utc() + timedelta(days=n_day)
        end_utc = start_utc + timedelta(days=1)
        res = round(0.5 * self.get_forecast_pv_estimates(start_utc, end_utc, site=site, _use_data_field=_use_data_field), 4)
        return res

    def get_sites_total_kwh_forecast_day(self, n_day) -> Dict[str, Any]:
        res = {}
        if self.options.attr_brk_site:
            for site in self._sites:
                res[site['resource_id']] = self.get_total_kwh_forecast_day(n_day, site=site['resource_id'])
                for _data_field in ('pv_estimate', 'pv_estimate10', 'pv_estimate90'):
                    if self._estimen.get(_data_field): res[_data_field.replace('pv_','')+'-'+site['resource_id']] = self.get_total_kwh_forecast_day(n_day, site=site['resource_id'], _use_data_field=_data_field)
        for _data_field in ('pv_estimate', 'pv_estimate10', 'pv_estimate90'):
            if self._estimen.get(_data_field): res[_data_field.replace('pv_','')] = self.get_total_kwh_forecast_day(n_day, site=None, _use_data_field=_data_field)
        return res

    def get_forecast_list_slice(self, _data, start_utc, end_utc, search_past=False):
        """Return Solcast pv_estimates list slice [st_i, end_i) for interval [start_utc, end_utc)"""
        crt_i = -1
        st_i = -1
        end_i = len(_data)
        for crt_i in range(0 if search_past else self._forecasts_start_idx, end_i):
            d = _data[crt_i]
            d1 = d['period_start']
            d2 = d1 + timedelta(seconds=1800)
            # after the last segment
            if end_utc <= d1:
                end_i = crt_i
                break
            # first segment
            if start_utc < d2 and st_i == -1:
                st_i = crt_i
        # never found
        if st_i == -1:
            st_i = 0
            end_i = 0
        return st_i, end_i

    def get_forecast_pv_estimates(self, start_utc, end_utc, site=None, _use_data_field=None, interpolate=False) -> float:
        """Return Solcast pv_estimates for interval [start_utc, end_utc)"""
        try:
            _data = self._data_forecasts if site is None else self._site_data_forecasts[site]
            _data_field = self._use_data_field if _use_data_field is None else _use_data_field
            res = 0
            st_i, end_i = self.get_forecast_list_slice(_data, start_utc, end_utc)
            def pchip(xx, i):
                x = [-1800, 0, 1800, 3600, ]
                y = [_data[i-1][_data_field] + _data[i][_data_field], _data[i][_data_field], 0, -1 * _data[i+1][_data_field], ]
                partial = cubic_interp([xx], x, y)[0]
                return partial if partial > 0 else 0
            # Calculate remaining
            for d in _data[st_i:end_i]:
                d1 = d['period_start']
                d2 = d1 + timedelta(seconds=1800)
                if not interpolate:
                    s = 1800
                f = d[_data_field]
                if start_utc > d1:
                    if not interpolate:
                        s -= (start_utc - d1).total_seconds()
                    else:
                        f = pchip((start_utc - d1).total_seconds(), st_i)
                if end_utc < d2:
                    if not interpolate:
                        s -= (d2 - end_utc).total_seconds()
                    else:
                        f = pchip((d2 - end_utc).total_seconds(), end_i)
                if not interpolate and s < 1800:
                    f *= s / 1800 # Simple linear interpolation
                res += f
            if _SENSOR_DEBUG_LOGGING: _LOGGER.debug("Get estimate: %s()%s st %s end %s st_i %d end_i %d res %s",
                          currentFuncName(1),
                          '' if site is None else ' '+site,
                          start_utc.strftime('%Y-%m-%d %H:%M:%S'),
                          end_utc.strftime('%Y-%m-%d %H:%M:%S'),
                          st_i, end_i, round(res,3))
            return res
        except Exception as ex:
            _LOGGER.error(f"Exception in get_forecast_pv_estimates(): {ex}")
            _LOGGER.error(traceback.format_exc())
            return 0

    def get_max_forecast_pv_estimate(self, start_utc, end_utc, site=None, _use_data_field=None):
        """Return max Solcast pv_estimate for the interval [start_utc, end_utc)"""
        try:
            _data = self._data_forecasts if site is None else self._site_data_forecasts[site]
            _data_field = self._use_data_field if _use_data_field is None else _use_data_field
            res = None
            st_i, end_i = self.get_forecast_list_slice(_data, start_utc, end_utc)
            for d in _data[st_i:end_i]:
                if res is None or res[_data_field] < d[_data_field]:
                    res = d
            if _SENSOR_DEBUG_LOGGING: _LOGGER.debug("Get max estimete: %s()%s st %s end %s st_i %d end_i %d res %s",
                          currentFuncName(1),
                          '' if site is None else ' '+site,
                          start_utc.strftime('%Y-%m-%d %H:%M:%S'),
                          end_utc.strftime('%Y-%m-%d %H:%M:%S'),
                          st_i, end_i, res)
            return res
        except Exception as ex:
            _LOGGER.error(f"Exception in get_max_forecast_pv_estimate(): {ex}")
            _LOGGER.error(traceback.format_exc())
            return None

    def get_energy_data(self) -> dict[str, Any]:
        try:
            return self._dataenergy
        except Exception as ex:
            _LOGGER.error(f"Exception in get_energy_data(): {ex}")
            _LOGGER.error(traceback.format_exc())
            return None

    async def http_data(self, dopast = False):
        """Request forecast data via the Solcast API."""
        try:
            if self.get_last_updated_datetime() + timedelta(minutes=15) > dt.now(timezone.utc):
                _LOGGER.warning(f"Not requesting a forecast from Solcast because time is within fifteen minutes of last update ({self.get_last_updated_datetime().astimezone(self._tz)})")
                return

            failure = False
            sitesAttempted = 0
            for site in self._sites:
                sitesAttempted += 1
                _LOGGER.info(f"Getting forecast update for Solcast site {site['resource_id']}")
                result = await self.http_data_call(self.get_api_usage_cache_filename(site['apikey']), site['resource_id'], site['apikey'], dopast)
                if not result:
                    failure = True

            if sitesAttempted > 0 and not failure:
                self._data["last_updated"] = dt.now(timezone.utc).isoformat()
                #self._data["weather"] = self._weather

                await self.buildforecastdata()
                self._data["version"] = _JSON_VERSION
                self._loaded_data = True

                await self.serialize_data()
            else:
                if sitesAttempted > 0:
                    _LOGGER.error("At least one Solcast site forecast failed to fetch, so forecast data has not been built")
                else:
                    _LOGGER.error("No Solcast sites were attempted, so forecast data has not been built - check for earlier failure to retrieve sites")
        except Exception as ex:
            _LOGGER.error("Exception in http_data(): %s - Forecast data has not been built", ex)
            _LOGGER.error(traceback.format_exc())

    async def http_data_call(self, usageCacheFileName, r_id = None, api = None, dopast = False):
        """Request forecast data via the Solcast API"""
        try:
            lastday = self.get_day_start_utc() + timedelta(days=8)
            numhours = math.ceil((lastday - self.get_now_utc()).total_seconds() / 3600)
            _LOGGER.debug(f"Polling API for site {r_id} lastday {lastday} numhours {numhours}")

            _data = []
            _data2 = []

            # This is run once, for a new install or if the solcast.json file is deleted
            # This does use up an api call count too
            if dopast:
                ae = None
                resp_dict = await self.fetch_data(usageCacheFileName, "estimated_actuals", 168, site=r_id, apikey=api, cachedname="actuals")
                if not isinstance(resp_dict, dict):
                    _LOGGER.error(f"No data was returned for Solcast estimated_actuals so this WILL cause errors...")
                    _LOGGER.error(f"Either your API limit is exhaused, Internet down, or networking is misconfigured...")
                    _LOGGER.error(f"This almost certainly not a problem with the integration, and sensor values will be wrong"
                    )
                    raise TypeError(f"Solcast API did not return a json object. Returned {resp_dict}")

                ae = resp_dict.get("estimated_actuals", None)

                if not isinstance(ae, list):
                    raise TypeError(f"estimated actuals must be a list, not {type(ae)}")

                oldest = dt.now(self._tz).replace(hour=0,minute=0,second=0,microsecond=0) - timedelta(days=6)
                oldest = oldest.astimezone(timezone.utc)

                for x in ae:
                    z = parse_datetime(x["period_end"]).astimezone(timezone.utc)
                    z = z.replace(second=0, microsecond=0) - timedelta(minutes=30)
                    if z.minute not in {0, 30}:
                        raise ValueError(
                            f"Solcast period_start minute is not 0 or 30. {z.minute}"
                        )
                    if z > oldest:
                        _data2.append(
                            {
                                "period_start": z,
                                "pv_estimate": x["pv_estimate"],
                                "pv_estimate10": 0,
                                "pv_estimate90": 0,
                            }
                        )

            resp_dict = await self.fetch_data(usageCacheFileName, "forecasts", numhours, site=r_id, apikey=api, cachedname="forecasts")
            if resp_dict is None:
                return False

            if not isinstance(resp_dict, dict):
                raise TypeError(f"Solcast API did not return a json object. Returned {resp_dict}")

            af = resp_dict.get("forecasts", None)
            if not isinstance(af, list):
                raise TypeError(f"forecasts must be a list, not {type(af)}")

            _LOGGER.debug(f"Solcast returned {len(af)} records")

            st_time = time.time()
            for x in af:
                z = parse_datetime(x["period_end"]).astimezone(timezone.utc)
                z = z.replace(second=0, microsecond=0) - timedelta(minutes=30)
                if z.minute not in {0, 30}:
                    raise ValueError(
                        f"Solcast period_start minute is not 0 or 30. {z.minute}"
                    )
                if z < lastday:
                    _data2.append(
                        {
                            "period_start": z,
                            "pv_estimate": x["pv_estimate"],
                            "pv_estimate10": x["pv_estimate10"],
                            "pv_estimate90": x["pv_estimate90"],
                        }
                    )

            _data = sorted(_data2, key=itemgetter("period_start"))
            _fcasts_dict = {}

            try:
                for x in self._data['siteinfo'][r_id]['forecasts']:
                    _fcasts_dict[x["period_start"]] = x
            except:
                pass

            _LOGGER.debug("Forecasts dictionary length %s", len(_fcasts_dict))

            for x in _data:
                #loop each site and its forecasts

                itm = _fcasts_dict.get(x["period_start"])
                if itm:
                    itm["pv_estimate"] = x["pv_estimate"]
                    itm["pv_estimate10"] = x["pv_estimate10"]
                    itm["pv_estimate90"] = x["pv_estimate90"]
                else:
                    _fcasts_dict[x["period_start"]] = {"period_start": x["period_start"],
                                                            "pv_estimate": x["pv_estimate"],
                                                            "pv_estimate10": x["pv_estimate10"],
                                                            "pv_estimate90": x["pv_estimate90"]}

            #_fcasts_dict now contains all data for the site up to 730 days worth
            #this deletes data that is older than 730 days (2 years)
            pastdays = dt.now(timezone.utc).date() + timedelta(days=-730)
            _forecasts = list(filter(lambda x: x["period_start"].date() >= pastdays, _fcasts_dict.values()))

            _forecasts = sorted(_forecasts, key=itemgetter("period_start"))

            self._data['siteinfo'].update({r_id:{'forecasts': copy.deepcopy(_forecasts)}})

            _LOGGER.debug(f"HTTP data call processing took {round(time.time()-st_time,4)}s")
            return True
        except Exception as ex:
            _LOGGER.error("Exception in http_data_call(): %s", ex)
            _LOGGER.error(traceback.format_exc())
        return False


    async def fetch_data(self, usageCacheFileName, path="error", hours=168, site="", apikey="", cachedname="forcasts") -> dict[str, Any]:
        """fetch data via the Solcast API."""
        try:
            params = {"format": "json", "api_key": apikey, "hours": hours}
            url=f"{self.options.host}/rooftop_sites/{site}/{path}"
            _LOGGER.debug(f"Fetch data url: {url}")

            async with async_timeout.timeout(480):
                apiCacheFileName = '/config/' + cachedname + "_" + site + ".json"
                if self.apiCacheEnabled and file_exists(apiCacheFileName):
                    status = 404
                    async with aiofiles.open(apiCacheFileName) as f:
                        resp_json = json.loads(await f.read())
                        status = 200
                        _LOGGER.debug(f"Got cached file data for site {site}")
                else:
                    if self._api_used[apikey] < self._api_limit[apikey]:
                        tries = 5
                        counter = 1
                        backoff = 30
                        while counter <= 5:
                            _LOGGER.debug(f"Fetching forecast")
                            resp: ClientResponse = await self.aiohttp_session.get(
                                url=url, params=params, ssl=False
                            )
                            status = resp.status
                            if status == 200: break
                            if status == 429:
                                # Solcast is busy, so delay (30 seconds * counter), plus a random number of seconds between zero and 30
                                delay = (counter * backoff) + random.randrange(0,30)
                                _LOGGER.warning(f"The Solcast API is busy, pausing {delay} seconds before retry")
                                await asyncio.sleep(delay)
                                counter += 1

                        if status == 200:
                            _LOGGER.debug(f"Fetch successful")

                            _LOGGER.debug(f"API returned data. API Counter incremented from {self._api_used[apikey]} to {self._api_used[apikey] + 1}")
                            self._api_used[apikey] = self._api_used[apikey] + 1
                            await self.write_api_usage_cache_file(usageCacheFileName,
                                {"daily_limit": self._api_limit[apikey], "daily_limit_consumed": self._api_used[apikey]},
                                apikey)

                            resp_json = await resp.json(content_type=None)

                            if self.apiCacheEnabled:
                                async with aiofiles.open(apiCacheFileName, 'w') as f:
                                    await f.write(json.dumps(resp_json, ensure_ascii=False))
                        else:
                            _LOGGER.error(f"Solcast API returned status {translate(status)}. API used is {self._api_used[apikey]}/{self._api_limit[apikey]}")
                    else:
                        _LOGGER.warning(f"API limit exceeded, not getting forecast")
                        return None

                _LOGGER.debug(f"HTTP ssession returned data type in fetch_data() is {type(resp_json)}")
                _LOGGER.debug(f"HTTP session status in fetch_data() is {translate(status)}")

            if status == 429:
                _LOGGER.warning("Solcast is too busy or exceeded API allowed polling limit, API used is {self._api_used[apikey]}/{self._api_limit[apikey]}")
            elif status == 400:
                _LOGGER.warning(
                    "Status {translate(status)}: The Solcast site is likely missing capacity, please specify capacity or provide historic data for tuning."
                )
            elif status == 404:
                _LOGGER.error(f"The Solcast site cannot be found, status {translate(status)} returned")
            elif status == 200:
                d = cast(dict, resp_json)
                _LOGGER.debug(f"Status {translate(status)} in fetch_data(), returned: {d}")
                return d
                #await self.format_json_data(d)
        except ConnectionRefusedError as err:
            _LOGGER.error("Connection error in fetch_data(), connection refused: %s", err)
        except ClientConnectionError as e:
            _LOGGER.error("Connection error in fetch_data(): %s", str(e))
        except asyncio.TimeoutError:
            _LOGGER.error("Connection error in fetch_data(): Timed out connectng to Solcast API server")
        except Exception as e:
            _LOGGER.error("Exception in fetch_data(): %s", traceback.format_exc())

        return None

    def makeenergydict(self) -> dict:
        wh_hours = {}

        try:
            lastv = -1
            lastk = -1
            for v in self._data_forecasts:
                d = v['period_start'].isoformat()
                if v[self._use_data_field] == 0.0:
                    if lastv > 0.0:
                        wh_hours[d] = round(v[self._use_data_field] * 500,0)
                        wh_hours[lastk] = 0.0
                    lastk = d
                    lastv = v[self._use_data_field]
                else:
                    if lastv == 0.0:
                        #add the last one
                        wh_hours[lastk] = round(lastv * 500,0)

                    wh_hours[d] = round(v[self._use_data_field] * 500,0)

                    lastk = d
                    lastv = v[self._use_data_field]
        except Exception as e:
            _LOGGER.error("Exception in makeenergydict(): %s", traceback.format_exc())

        return wh_hours

    async def buildforecastdata(self):
        """build the data needed and convert where needed"""
        try:
            today = dt.now(self._tz).date()
            yesterday = dt.now(self._tz).date() + timedelta(days=-730)
            lastday = dt.now(self._tz).date() + timedelta(days=8)

            _fcasts_dict = {}

            st_time = time.time()
            for site, siteinfo in self._data['siteinfo'].items():
                tally = 0
                _site_fcasts_dict = {}

                for x in siteinfo['forecasts']:
                    #loop each site and its forecasts
                    z = x["period_start"]
                    zz = z.astimezone(self._tz) #- timedelta(minutes=30)

                    #v4.0.8 added code to dampen the forecast data: (* self._damp[h])

                    if yesterday < zz.date() < lastday:
                        h = f"{zz.hour}"
                        if zz.date() == today:
                            tally += min(x[self._use_data_field] * 0.5 * self._damp[h], self._hardlimit)

                        # add the dampened forecast for this site to the total
                        itm = _fcasts_dict.get(z)
                        if itm:
                            itm["pv_estimate"] = min(round(itm["pv_estimate"] + (x["pv_estimate"] * self._damp[h]),4), self._hardlimit)
                            itm["pv_estimate10"] = min(round(itm["pv_estimate10"] + (x["pv_estimate10"] * self._damp[h]),4), self._hardlimit)
                            itm["pv_estimate90"] = min(round(itm["pv_estimate90"] + (x["pv_estimate90"] * self._damp[h]),4), self._hardlimit)
                        else:
                            _fcasts_dict[z] = {"period_start": z,
                                                "pv_estimate": min(round((x["pv_estimate"]* self._damp[h]),4), self._hardlimit),
                                                "pv_estimate10": min(round((x["pv_estimate10"]* self._damp[h]),4), self._hardlimit),
                                                "pv_estimate90": min(round((x["pv_estimate90"]* self._damp[h]),4), self._hardlimit)}

                        # record the individual site forecast
                        _site_fcasts_dict[z] = {"period_start": z,
                                            "pv_estimate": min(round((x["pv_estimate"]* self._damp[h]),4), self._hardlimit),
                                            "pv_estimate10": min(round((x["pv_estimate10"]* self._damp[h]),4), self._hardlimit),
                                            "pv_estimate90": min(round((x["pv_estimate90"]* self._damp[h]),4), self._hardlimit)}
                self._site_data_forecasts[site] = sorted(_site_fcasts_dict.values(), key=itemgetter("period_start"))

                siteinfo['tally'] = round(tally, 4)
                self._tally[site] = siteinfo['tally']

            self._data_forecasts = sorted(_fcasts_dict.values(), key=itemgetter("period_start"))

            self._forecasts_start_idx = self.calcForecastStartIndex()

            self._dataenergy = {"wh_hours": self.makeenergydict()}

            await self.checkDataRecords()

            _LOGGER.debug(f"Build forecast processing took {round(time.time()-st_time,4)}s")

        except Exception as e:
            _LOGGER.error("Exception in http_data(): %s", traceback.format_exc())


    def calcForecastStartIndex(self):
        midnight_utc = self.get_day_start_utc()
        # search in reverse (less to iterate) and find the interval just before midnight
        # we could stop at midnight but some sensors might need the previous interval
        for idx in range(len(self._data_forecasts)-1, -1, -1):
            if self._data_forecasts[idx]["period_start"] < midnight_utc: break
        _LOGGER.debug("Calc forecast start index midnight utc: %s, idx %s, len %s", midnight_utc, idx, len(self._data_forecasts))
        return idx


    async def checkDataRecords(self):
        for i in range(0,8):
            start_utc = self.get_day_start_utc() + timedelta(days=i)
            end_utc = start_utc + timedelta(days=1)
            st_i, end_i = self.get_forecast_list_slice(self._data_forecasts, start_utc, end_utc)
            num_rec = end_i - st_i

            da = dt.now(self._tz).date() + timedelta(days=i)
            if num_rec == 48:
                _LOGGER.debug(f"Data for {da} contains all 48 records")
            else:
                _LOGGER.debug(f"Data for {da} contains only {num_rec} of 48 records and may produce inaccurate forecast data")
