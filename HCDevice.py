#!/usr/bin/env python3
# Parse messages from a Home Connect websocket (HCSocket)
# and keep the connection alive
#
# Possible resources to fetch from the devices:
#
# /ro/values
# /ro/descriptionChange
# /ro/allMandatoryValues
# /ro/allDescriptionChanges
# /ro/activeProgram
# /ro/selectedProgram
#
# /ei/initialValues
# /ei/deviceReady
#
# /ci/services
# /ci/registeredDevices
# /ci/pairableDevices
# /ci/delregistration
# /ci/networkDetails
# /ci/networkDetails2
# /ci/wifiNetworks
# /ci/wifiSetting
# /ci/wifiSetting2
# /ci/tzInfo
# /ci/authentication
# /ci/register
# /ci/deregister
#
# /ce/serverDeviceType
# /ce/serverCredential
# /ce/clientCredential
# /ce/hubInformation
# /ce/hubConnected
# /ce/status
#
# /ni/config
# /ni/info
#
# /iz/services

import json
import re
import sys
import traceback
from base64 import urlsafe_b64encode as base64url_encode
from datetime import datetime

from Crypto.Random import get_random_bytes


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")


class HCDevice:
    def __init__(self, ws, features, name):
        self.ws = ws
        self.features = features
        self.session_id = None
        self.tx_msg_id = None
        self.device_name = "hcpy"
        self.device_id = "0badcafe"
        self.debug = False
        self.name = name
        self.services_initialized = False
        self.services = {}
        self.token = None

    def parse_values(self, values):
        if not self.features:
            return values

        result = {}

        for msg in values:
            uid = str(msg["uid"])
            value = msg["value"]
            value_str = str(value)

            name = uid
            status = None

            if uid in self.features:
                status = self.features[uid]

            if status:
                name = status["name"]
                if "values" in status and value_str in status["values"]:
                    value = status["values"][value_str]

            # trim everything off the name except the last part
            name = re.sub(r"^.*\.", "", name)
            result[name] = value

        return result

    # Based on PR submitted https://github.com/Skons/hcpy/pull/1
    def test_program_data(self, data_array):
        for data in data_array:
            if "program" not in data:
                raise TypeError("Message data invalid, no program specified.")

            if isinstance(data["program"], int) is False:
                raise TypeError("Message data invalid, UID in 'program' must be an integer.")

            # devices.json stores UID as string
            uid = str(data["program"])
            if uid not in self.features:
                raise ValueError(
                    f"Unable to configure appliance. Program UID {uid} is not valid"
                    " for this device."
                )

            feature = self.features[uid]
            # Diswasher is Dishcare.Dishwasher.Program.{name}
            # Hood is Cooking.Common.Program.{name}
            # May also be in the format BSH.Common.Program.Favorite.001
            if ".Program." not in feature["name"]:
                raise ValueError(
                    f"Unable to configure appliance. Program UID {uid} is not a valid"
                    f" program - {feature['name']}."
                )

            if "options" in data:
                for option in data["options"]:
                    option_uid = option["uid"]
                    if str(option_uid) not in self.features:
                        raise ValueError(
                            f"Unable to configure appliance. Option UID {option_uid} is not"
                            " valid for this device."
                        )

    # Test the feature of an appliance agains a data object
    def test_feature(self, data_array):
        for data in data_array:
            if "uid" not in data:
                raise Exception("Unable to configure appliance. UID is required.")

            if isinstance(data["uid"], int) is False:
                raise Exception("Unable to configure appliance. UID must be an integer.")

            if "value" not in data:
                raise Exception("Unable to configure appliance. Value is required.")

            # Check if the uid is present for this appliance
            uid = str(data["uid"])
            if uid not in self.features:
                raise Exception(f"Unable to configure appliance. UID {uid} is not valid.")

            feature = self.features[uid]

            # check the access level of the feature
            print(now(), self.name, f"Processing feature {feature['name']} with uid {uid}")
            if "access" not in feature:
                raise Exception(
                    "Unable to configure appliance. "
                    f"Feature {feature['name']} with uid {uid} does not have access."
                )

            access = feature["access"].lower()
            if access != "readwrite" and access != "writeonly":
                raise Exception(
                    "Unable to configure appliance. "
                    f"Feature {feature['name']} with uid {uid} has got access {feature['access']}."
                )

            # check if selected list with values is allowed
            if "values" in feature:
                if isinstance(data["value"], int) is False:
                    raise Exception(
                        f"Unable to configure appliance. The value {data['value']} must "
                        f"be an integer. Allowed values are {feature['values']}."
                    )

                value = str(data["value"])
                # values are strings in the feature list,
                # but always seem to be an integer. An integer must be provided
                if value not in feature["values"]:
                    raise Exception(
                        "Unable to configure appliance. "
                        f"Value {data['value']} is not a valid value. "
                        f"Allowed values are {feature['values']}. "
                    )

            if "min" in feature:
                min = int(feature["min"])
                max = int(feature["max"])
                if (
                    isinstance(data["value"], int) is False
                    or data["value"] < min
                    or data["value"] > max
                ):
                    raise Exception(
                        "Unable to configure appliance. "
                        f"Value {data['value']} is not a valid value. "
                        f"The value must be an integer in the range {min} and {max}."
                    )

    def recv(self):
        try:
            buf = self.ws.recv()
            if buf is None:
                return None
        except Exception as e:
            print(self.name, "receive error", e, traceback.format_exc())
            return None

        try:
            return self.handle_message(buf)
        except Exception as e:
            print(self.name, "error handling msg", e, buf, traceback.format_exc())
            return None

    # reply to a POST or GET message with new data
    def reply(self, msg, reply):
        self.ws.send(
            {
                "sID": msg["sID"],
                "msgID": msg["msgID"],  # same one they sent to us
                "resource": msg["resource"],
                "version": msg["version"],
                "action": "RESPONSE",
                "data": [reply],
            }
        )

    # send a message to the device
    def get(self, resource, version=1, action="GET", data=None):
        if self.services_initialized:
            resource_parts = resource.split("/")
            if len(resource_parts) > 1:
                service = resource.split("/")[1]
                if service in self.services.keys():
                    version = self.services[service]["version"]
                else:
                    print(now(), self.name, "ERROR service not known")

        msg = {
            "sID": self.session_id,
            "msgID": self.tx_msg_id,
            "resource": resource,
            "version": version,
            "action": action,
        }

        if data is not None:
            if isinstance(data, list) is False:
                data = [data]

            if action == "POST":
                if resource == "/ro/values":
                    # Raises exceptions on failure
                    self.test_feature(data)
                elif resource == "/ro/activeProgram":
                    # Raises exception on failure
                    self.test_program_data(data)

            msg["data"] = data

        try:
            self.ws.send(msg)
        except Exception as e:
            print(self.name, "Failed to send", e, msg, traceback.format_exc())
        self.tx_msg_id += 1

    def reconnect(self):
        self.ws.reconnect()
        # Receive initialization message /ei/initialValues
        # Automatically responds in the handle_message function
        self.recv()

        # ask the device which services it supports
        # registered devices gets pushed down too hence the loop
        self.get("/ci/services")
        while True:
            self.recv()
            if self.services_initialized:
                break

        # We override the version based on the registered services received above

        # the clothes washer wants this, the token doesn't matter,
        # although they do not handle padding characters
        # they send a response, not sure how to interpet it
        self.token = base64url_encode(get_random_bytes(32)).decode("UTF-8")
        self.token = re.sub(r"=", "", self.token)
        self.get("/ci/authentication", version=2, data={"nonce": self.token})

        self.get("/ci/info")  # clothes washer
        self.get("/iz/info")  # dish washer

        # Retrieves registered clients like phone/hcpy itself
        self.get("/ci/registeredDevices")

        # tzInfo all returns empty?
        # self.get("/ci/tzInfo")

        # We need to send deviceReady for some devices or /ni/ will come back as 403 unauth
        self.get("/ei/deviceReady", version=2, action="NOTIFY")
        self.get("/ni/info")
        # self.get("/ni/config", data={"interfaceID": 0})

        # self.get("/ro/allDescriptionChanges")
        self.get("/ro/allMandatoryValues")
        # self.get("/ro/values")

    def handle_message(self, buf):
        msg = json.loads(buf)
        if self.debug:
            print(now(), self.name, "RX:", msg)
        sys.stdout.flush()

        resource = msg["resource"]
        action = msg["action"]

        values = {}

        if "code" in msg:
            values = {
                "error": msg["code"],
                "resource": msg.get("resource", ""),
            }
        elif action == "POST":
            if resource == "/ei/initialValues":
                # this is the first message they send to us and
                # establishes our session plus message ids
                self.session_id = msg["sID"]
                self.tx_msg_id = msg["data"][0]["edMsgID"]

                self.reply(
                    msg,
                    {
                        "deviceType": "Application",
                        "deviceName": self.device_name,
                        "deviceID": self.device_id,
                    },
                )
            else:
                print(now(), self.name, "Unknown resource", resource, file=sys.stderr)

        elif action == "RESPONSE" or action == "NOTIFY":
            if resource == "/iz/info" or resource == "/ci/info":
                if "data" in msg and len(msg["data"]) > 0:
                    # Return Device Information such as Serial Number, SW Versions, MAC Address
                    values = msg["data"][0]

            elif resource == "/ro/descriptionChange" or resource == "/ro/allDescriptionChanges":
                ### we asked for these but don't know have to parse yet
                pass

            elif resource == "/ni/info":
                if "data" in msg and len(msg["data"]) > 0:
                    # Return Network Information/IP Address etc
                    values = msg["data"][0]

            elif resource == "/ni/config":
                # Returns some data about network interfaces e.g.
                # [{'interfaceID': 0, 'automaticIPv4': True, 'automaticIPv6': True}]
                pass

            elif resource == "/ro/allMandatoryValues" or resource == "/ro/values":
                if "data" in msg:
                    values = self.parse_values(msg["data"])
                else:
                    print(now(), self.name, f"received {msg}")

            elif resource == "/ci/registeredDevices":
                # This contains details of Phone/HCPY registered as clients to the device
                pass

            elif resource == "/ci/tzInfo":
                pass

            elif resource == "/ci/authentication":
                if "data" in msg and len(msg["data"]) > 0:
                    # Grab authentication token - unsure if this is for us to use
                    # or to authenticate the server. Doesn't appear to be needed
                    self.token = msg["data"][0]["response"]

            elif resource == "/ci/services":
                for service in msg["data"]:
                    self.services[service["service"]] = {
                        "version": service["version"],
                    }
                self.services_initialized = True

            else:
                print(now(), self.name, "Unknown response or notify:", msg)

        else:
            print(now(), self.name, "Unknown message", msg)

        # return whatever we've parsed out of it
        return values
