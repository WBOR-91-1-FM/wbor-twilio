"""
Twilio Handler.

Note: This app requires more than one worker process to work properly.

We have a Twilio phone number that can receive SMS messages & more.
When a SMS message is received at this number, the primary handler is
configured as this Flask app. If this app doesn't respond with an OK
status code, Twilio will fall back to the secondary handler (which is a
Twilio function).

This app has two primary functions:
1. Publish incoming SMS messages to RabbitMQ for processing by other
    services.
2. Send outgoing SMS messages using the Twilio API.

Future functionality may include (e.g. logging in PG):
- Handling incoming voice intelligence data (at /voice-intelligence).
- Handling incoming call events (at /call-events).

The workflows are as follows.

Incoming SMS:
1. A SMS message is received by Twilio.
2. Twilio sends the message to this app at the `/sms` endpoint.
3. @validate_twilio_request decorator validates the authenticity of the
    request.
4. A unique message ID is generated and added to the message data as
    `wbor_message_id`.
5. An acknowledgment event is set in Redis with the message ID.
    - This is used to ensure the message is processed properly
        downstream by wbor-groupme.
    - (Upon receipt from the consumer of successful forwarding to
        GroupMe, response is sent to Twilio.)
6. Twilio phone number lookup is used to fetch the name of the sender
    and added to the message data if available as `SenderName`. If the
    lookup fails, the sender name is set to `Unknown`.
    - If the lookup fails, the sender name is set to `Unknown`.
7. The message data is published to RabbitMQ with the routing key
    `source.twilio.sms.incoming`.
    - Sent to `source_exchange` (after asserting that it exists)
    - Routing key is in the format `source.<source>.<type>`
        - e.g. `source.twilio.sms.incoming` or
            `source.twilio.sms.outgoing`
    - The message type is also included in the JSON message body for
        downstream consumers.
8. The main thread waits for an acknowledgment from the `/acknowledge`
    endpoint, indicating successful processing by GroupMe.
    - If the acknowledgment is not received within a timeout, the
        message is discarded.
    - Subsequently, Twilio will fall back to the secondary handler.
    - NOTE: `/acknowledge` requires more than one worker process to work
        properly.
    - NOTE: `/acknowledge` does not validate the SOURCE of the
        acknowledgment, which is a potential security risk (though
        unlikely in our closed network).
9. Upon receiving the acknowledgment, return an empty TwiML response to
    Twilio to acknowledge receipt of the message.

Outgoing SMS:
0. Upon launching the app, a consumer thread is started to listen for
    outgoing SMS messages.
    - The consumer listens for messages with the routing key
        'source.twilio.sms.outgoing'.
1. A GET request is made using the `/send` endpoint in a browser.
    - Expects a password for authorization set by APP_PASSWORD.
    - Expects `recipient_number` and `body` as query parameters.
2. Validates the recipient number and message body.
    - `recipient_number` must be in E.164 format.
    - `body` must not exceed the Twilio character limit.
    - `body` body must exist.
3. Generates a unique message ID for tracking, set as `wbor_message_id`.
4. Prepares the outgoing message data, including a timestamp.
5. Publishes it to RabbitMQ with the routing key
    `source.twilio.sms.outgoing`.
6. The consumer thread consumes by running process_outgoing_message().

Emits keys:
- `source.twilio.sms.incoming`
    - Routed to wbor-groupme for processing.
- `source.twilio.sms.outgoing`
    - Local queue for sending outgoing SMS messages.
- `source.twilio.voice-intelligence`
- `source.twilio.call-events`

TODO: fix not shutting down when MQ connection breaks?
"""

import json
import logging
import os
import re
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from functools import wraps
from threading import Thread
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from uuid import uuid4

import pika
import requests
from flask import Flask, abort, request
from pika.exceptions import AMQPChannelError, AMQPConnectionError, ChannelClosedByBroker
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse

from config import (
    APP_PASSWORD,
    APP_PORT,
    OUTGOING_QUEUE,
    RABBITMQ_EXCHANGE,
    RABBITMQ_HOST,
    RABBITMQ_PASS,
    RABBITMQ_USER,
    REDIS_ACK_EXPIRATION_S,
    SMS_OUTGOING_KEY,
    SOURCE,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_CHARACTER_LIMIT,
    TWILIO_PHONE_NUMBER,
)
from utils.logging import configure_logging
from utils.redis import delete_ack_event, get_ack_event, redis_client, set_ack_event

logging.root.handlers = []
logger = configure_logging()

app = Flask(__name__)

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
twilio_client.http_client.logger.setLevel(logging.INFO)


def terminate(exit_code: int = 1) -> None:
    """
    Terminate the process.
    """
    os.kill(os.getppid(), signal.SIGTERM)  # Gunicorn master
    os._exit(exit_code)  # Current thread


# RabbitMQ


def publish_to_exchange(key: str, sub_key: str, data: dict) -> None:
    """
    Publishes a message to a RabbitMQ exchange.

    Parameters:
    - key (str): The name of the message key.
    - sub_key (str): The name of the sub-key for the message. (e.g.
        'sms', 'call')
    - data (dict): The message content, which will be converted to JSON
        format.
    """
    try:
        logger.debug("Attempting to connect to RabbitMQ...")
        credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
        parameters = pika.ConnectionParameters(
            host=RABBITMQ_HOST,
            credentials=credentials,
            client_properties={"connection_name": "TwilioConnection"},
        )
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()
        logger.debug("RabbitMQ connected!")

        # Assert the exchange exists
        channel.exchange_declare(
            exchange=RABBITMQ_EXCHANGE,
            exchange_type="topic",
            durable=True,
        )

        # Publish message to exchange
        channel.basic_publish(
            exchange=RABBITMQ_EXCHANGE,
            routing_key=f"source.{key}.{sub_key}",
            body=json.dumps(
                {**data, "type": sub_key}  # Include type in message body
            ).encode(),  # Encodes msg as bytes RabbitMQ requires bytes
            properties=pika.BasicProperties(
                headers={
                    "x-retry-count": 0
                },  # Initialize retry count for other consumers
                delivery_mode=2,  # Persistent message, write for safety
            ),
        )
        logger.debug(
            "Publishing message body: `%s`", json.dumps({**data, "type": sub_key})
        )
        # Handle difference between Twilio incoming/outgoing messages
        message_body_content = data.get("Body") or data.get("body")
        logger.info(
            "Published message to `%s` with routing key: `source.%s.%s`: "
            "Sender: `%s` - `%s` - UID: `%s`",
            RABBITMQ_EXCHANGE,
            key,
            sub_key,
            data.get("SenderName", "Unknown"),
            message_body_content,
            data.get("wbor_message_id"),
        )
        connection.close()
    except AMQPConnectionError as conn_error:
        error_message = str(conn_error)
        logger.error(
            "Connection error when publishing to `%s` with routing key "
            "`source.%s.%s`: %s",
            RABBITMQ_EXCHANGE,
            key,
            sub_key,
            error_message,
        )
        if "CONNECTION_FORCED" in error_message and "shutdown" in error_message:
            logger.critical("Broker shut down the connection. Shutting down consumer.")
            sys.exit(1)
        if "ACCESS_REFUSED" in error_message:
            logger.critical(
                "Access refused. Check RabbitMQ user permissions. Shutting down consumer."
            )
        terminate()
    except AMQPChannelError as chan_error:
        logger.error(
            "Channel error when publishing to `%s` with routing key `source.%s.%s`: `%s`",
            RABBITMQ_EXCHANGE,
            key,
            sub_key,
            chan_error,
        )
    except json.JSONDecodeError as json_error:
        logger.error("JSON encoding error for message `%s`: `%s`", data, json_error)


def validate_twilio_request(f: callable) -> callable:
    """
    Validates that incoming requests genuinely originated from Twilio
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):
        validator = RequestValidator(TWILIO_AUTH_TOKEN)

        # Parse+reconstruct the URL to ensure query strings are encoded
        parsed_url = urlparse(request.url)
        query = urlencode(parse_qsl(parsed_url.query, keep_blank_values=True))

        # Ensure the path includes /twilio prefix
        # This is a hack because I couldn't figure out how to get NGINX
        # to not strip /twilio when putting this app behind a proxy
        # instead of serving at the root
        path_with_prefix = parsed_url.path
        if not path_with_prefix.startswith("/twilio"):
            path_with_prefix = "/twilio" + path_with_prefix

        encoded_url = urlunparse(
            (
                parsed_url.scheme.replace("http", "https"),
                parsed_url.netloc,
                path_with_prefix,  # Use the adjusted path
                parsed_url.params,
                query,
                parsed_url.fragment,
            )
        )

        # Validate request
        request_valid = validator.validate(
            encoded_url, request.form, request.headers.get("X-TWILIO-SIGNATURE", "")
        )

        if not request_valid:
            logger.error("Twilio request validation failed!")
            logger.debug("Form data used for validation: `%s`", request.form)
            return abort(403)
        return f(*args, **kwargs)

    return decorated_function


def fetch_name(sms_data: dict) -> str:
    """
    Attempt to fetch the name associated with a phone number using the
    Twilio Lookup API.

    caller_name is the name associated with the phone number in the
    Twilio database. If the name is not available, it returns 'Unknown'.

    Don't confuse this with the 'From' field in the SMS data, which is
    the phone number.

    Parameters:
    - sms_data (dict): The SMS message data containing the sender's
        phone number.

    Returns:
    - str: The name of the sender if available, otherwise 'Unknown'.
    """
    phone_number = sms_data.get("From")
    if not phone_number:
        logger.warning("No `From` field in SMS data: `%s`", sms_data)
        return "Unknown"

    try:
        phone_info = twilio_client.lookups.v2.phone_numbers(phone_number).fetch(
            fields="caller_name"
        )

        caller_name = phone_info.caller_name or "Unknown"

        if caller_name.get("caller_name", None) is None:
            logger.debug("No caller name found for number: `%s`", phone_number)
            return "Unknown"
        else:
            logger.info(
                "Fetched name: `%s`",
                caller_name.get("caller_name", "Unknown (this shouldn't happen)"),
            )
        return caller_name.get("caller_name", "Unknown")
    except TwilioRestException as e:
        logger.error(
            "Failed to fetch caller name for number `%s`: `%s`", phone_number, str(e)
        )
        return "Unknown"


def process_outgoing_sms_message(
    channel: pika.channel.Channel,
    method: pika.spec.Basic.Deliver,
    _properties: pika.spec.BasicProperties,
    body: bytes,
) -> None:
    """
    Processes an outgoing SMS message from RabbitMQ.
    """
    logger.debug("Received message with routing key: `%s`", method.routing_key)

    # Validate routing key
    if method.routing_key != SMS_OUTGOING_KEY:
        logger.warning(
            "Discarding message due to mismatched routing key: `%s` (expecting `%s`)",
            method.routing_key,
            SMS_OUTGOING_KEY,
        )
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    try:
        message = json.loads(body)
        recipient_number = message.get("recipient_number")
        sms_body = message.get("body")
        wbor_message_id = message.get("wbor_message_id")

        if not recipient_number:
            logger.warning(
                "Invalid message format (missing `recipient_number`): `%s`", message
            )
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return
        if not sms_body:
            logger.warning("Invalid message format (missing `body`): `%s`", message)
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        # Attempt to send the SMS
        msg_sid = send_sms(recipient_number, sms_body, wbor_message_id)
        if msg_sid:
            logger.info(
                "Message sent successfully. UID: `%s`, SID: `%s`",
                message.get("wbor_message_id"),
                msg_sid,
            )
            channel.basic_ack(delivery_tag=method.delivery_tag)
        else:
            logger.error(
                "Failed to send SMS for UID: `%s`", message.get("wbor_message_id")
            )
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
    except (json.JSONDecodeError, KeyError, ValueError) as specific_error:
        logger.exception(
            "Unhandled exception while processing message: `%s`", specific_error
        )
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


def send_sms(
    recipient_number: str, message_body: str, wbor_message_id: str = None
) -> str:
    """
    Sends an SMS message using the Twilio API.
    Logs calls to Postgres.

    Parameters:
    - recipient_number (str): The phone number to send the message to
        (in E.164 format).
    - message_body (str): The body of the SMS message.

    Returns:
    - str: The SID of the sent message if successful.

    Raises:
    - Exception: If the message fails to send.
    """
    if not recipient_number or not message_body:
        logger.error("Recipient number or message body cannot be empty")
        raise ValueError("Recipient number and message body are required")

    if len(message_body) > TWILIO_CHARACTER_LIMIT:
        logger.error(
            "Message body exceeds Twilio's character limit of `%d`",
            TWILIO_CHARACTER_LIMIT,
        )
        raise ValueError(
            f"Message exceeds the character limit of {TWILIO_CHARACTER_LIMIT}"
        )

    try:
        logger.debug(
            "Attempting to send SMS to `%s` - UID: `%s`",
            recipient_number,
            wbor_message_id,
        )
        message = twilio_client.messages.create(
            to=recipient_number,
            from_=TWILIO_PHONE_NUMBER,
            body=message_body,
        )
        return message.sid
    except TwilioRestException as e:
        logger.error(
            "Failed to send SMS to `%s`: `%s` - UID: `%s`",
            recipient_number,
            str(e),
            wbor_message_id,
        )
        return None


def start_outgoing_message_consumer() -> None:
    """
    Starts a RabbitMQ consumer for the outgoing message queue.
    Handles sending SMS messages using the Twilio API.
    """

    def consumer_thread() -> None:
        while True:
            try:
                logger.debug("Connecting to RabbitMQ for outgoing SMS messages...")
                credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
                parameters = pika.ConnectionParameters(
                    host=RABBITMQ_HOST,
                    credentials=credentials,
                    client_properties={
                        "connection_name": "OutgoingSMSConsumerConnection"
                    },
                )
                connection = pika.BlockingConnection(parameters)
                channel = connection.channel()

                # Assert that the primary exchange exists
                channel.exchange_declare(
                    exchange=RABBITMQ_EXCHANGE, exchange_type="topic", durable=True
                )

                try:
                    # Declare the queue
                    channel.queue_declare(queue=OUTGOING_QUEUE, durable=True)
                    logger.debug("Queue declared: `%s`", OUTGOING_QUEUE)
                    channel.queue_bind(
                        queue=OUTGOING_QUEUE,
                        exchange=RABBITMQ_EXCHANGE,
                        routing_key=SMS_OUTGOING_KEY,  # Only bind to this key
                    )
                    logger.debug(
                        "Queue `%s` bound to `%s` with routing key `%s`",
                        OUTGOING_QUEUE,
                        RABBITMQ_EXCHANGE,
                        SMS_OUTGOING_KEY,
                    )
                except ChannelClosedByBroker as e:
                    if "inequivalent arg" in str(e):
                        # If the queue already exists with different
                        # attributes, log and terminate
                        logger.warning(
                            "Queue already exists with mismatched attributes. "
                            "Please resolve this conflict before restarting the application."
                        )
                        terminate()

                # Ensure one message is processed at a time
                channel.basic_qos(prefetch_count=1)
                channel.basic_consume(
                    queue=OUTGOING_QUEUE,
                    on_message_callback=process_outgoing_sms_message,
                )

                logger.info(
                    "Outgoing message consumer is ready. Waiting for messages..."
                )
                channel.start_consuming()
            except AMQPConnectionError as conn_error:
                error_message = str(conn_error)
                logger.error(
                    "Failed to connect to RabbitMQ: `%s`",
                    error_message,
                )
                if "CONNECTION_FORCED" in error_message and "shutdown" in error_message:
                    logger.critical(
                        "Broker shut down the connection. Shutting down consumer..."
                    )
                    sys.exit(1)
                if "ACCESS_REFUSED" in error_message:
                    logger.critical(
                        "Access refused. Check RabbitMQ user permissions. Shutting down process..."
                    )
                    terminate()
            finally:
                if "connection" in locals() and connection.is_open:
                    connection.close()

    Thread(target=consumer_thread, daemon=True).start()


# Routes


@app.route("/acknowledge", methods=["POST"])
def groupme_acknowledge() -> str:
    """
    Endpoint for receiving acknowledgment from the GROUPME_QUEUE
    consumer.

    Expects a JSON payload with a 'wbor_message_id' field indicating the
    message processed.
    """
    ack_data = request.json
    if not ack_data or "wbor_message_id" not in ack_data:
        logger.error(
            "Invalid acknowledgment data received at /acknowledge: `%s`", ack_data
        )
        return "Invalid acknowledgment", 400

    message_id = ack_data.get("wbor_message_id")
    logger.debug("Received acknowledgment for: `%s`", message_id)

    if get_ack_event(message_id):
        delete_ack_event(message_id)
        return "Acknowledgment received", 200

    logger.warning("Acknowledgment received for unknown: `%s`", message_id)
    return "Unknown wbor_message_id", 404


def has_media(sms_data: dict) -> bool:
    """
    Check for the presence of media in an SMS message.

    Parameters:
    - sms_data (dict): The SMS message data.

    Returns:
    - bool: True if the message contains media, False otherwise.
    """
    num_media = int(sms_data.get("NumMedia", 0))
    return num_media > 0


def has_unsupported_media(sms_data: dict) -> bool:
    """
    Check the media URLs in an SMS message for unsupported types.

    Parameters:
    - sms_data (dict): The SMS message data.

    Returns:
    - bool: True if the message contains unsupported media, False
        otherwise.

    Note:
    - Change list of supported MIME types as needed downstream.
    """
    num_media = int(sms_data.get("NumMedia", 0))
    if num_media == 0:
        # No media to check
        return False

    media_urls = [sms_data.get(f"MediaUrl{i}") for i in range(0, num_media)]
    if not any(media_urls):
        # If for some reason there are no URLs, no media to check
        return False

    supported_mime_types = {"image/jpeg", "image/png", "image/gif"}
    contains_invalid_media = False

    for url in media_urls:
        try:
            # Download the file and check the MIME type
            response = requests.get(url, timeout=10, allow_redirects=True)
            if response.status_code == 200:
                content_type = response.headers.get("Content-Type", "")
                logger.debug("Media URL: `%s`, Content-Type: `%s`", url, content_type)

                # Check if the MIME type is supported
                if content_type not in supported_mime_types:
                    contains_invalid_media = True
                    logger.warning("Unsupported media type: `%s`", content_type)
                    break
            else:
                logger.error(
                    "Failed to fetch media URL `%s`, status code: `%s`",
                    url,
                    response.status_code,
                )
                contains_invalid_media = True
                break
        except requests.RequestException as e:
            logger.error("Error fetching media URL `%s`: `%s`", url, e)
            contains_invalid_media = True
            break

    return contains_invalid_media


def get_automation_status() -> bool:
    """
    Check the status of the automation system.

    If the api isn't reachable, assume automation is disabled.

    Returns:
    - bool: True if automation is enabled, False otherwise.
    """
    try:
        response = requests.get("https://api-1.wbor.org/api/playlists", timeout=5)
        response.raise_for_status()
        items = response.json().get("items")
        curr_show = items[0] if items else {}
        auto_status = curr_show.get("automation")
        if auto_status is None:
            logger.warning("Automation status not found in response")
            return False
        if auto_status == 1:
            logger.debug("Automation is enabled")
            return True
        logger.debug("Automation is disabled")
        return False
    except requests.RequestException as e:
        logger.error("Error fetching automation status: `%s`", e)
        return False
    except (KeyError, IndexError) as e:
        logger.error("Error parsing automation status: `%s`", e)
        return False


@app.route("/sms", methods=["POST"])
@validate_twilio_request
def receive_sms() -> str:
    """
    Handler for incoming SMS messages from Twilio. Publishes messages to
    RabbitMQ.

    Returns:
    - str: A TwiML response to acknowledge receipt of the message
        (required by Twilio).

    Note:
    If a response is not returned, Twilio will fall back to the
    secondary message handler.
    """
    sms_data = request.form.to_dict()
    logger.debug("Received SMS message: `%s`", sms_data)
    logger.info("Processing message from: `%s`", sms_data.get("From"))
    resp = MessagingResponse()  # Required by Twilio

    is_automation = get_automation_status()

    if not is_automation:
        if has_media(sms_data):
            if has_unsupported_media(sms_data):
                # If any contain a type that is unsupported, update the message body to reflect this
                # and let the sender know that their message contains unsupported media and may not
                # be delivered as expected
                response_message = (
                    "Thank you for your message! Unfortunately, it contains "
                    "one or more unsupported media types. "
                    "As a result, it may not be delivered as expected. "
                    "- WBOR \n\n(Note: DJs cannot reply to texts.)"
                )
                resp.message(response_message)
            else:
                response_message = (
                    "Thank you for your message! Unfortunately, we don't support "
                    "media at this time, so the DJ won't see any photos or videos"
                    " sent. - WBOR \n\n(Note: DJs cannot reply to texts.)"
                )
                resp.message(response_message)
        else:
            response_message = (
                "Thank you for your message! - WBOR \n\n(Note: DJs cannot reply to "
                "texts.)"
            )
            resp.message(response_message)
    else:
        response_message = (
            "According to our records, we don't see a live DJ playlist active, "
            "so there might not currently be any DJs in the studio to receive "
            "your text :( \n\n"
            "Try again later or view our schedule at wbor.org/schedule"
        )
        resp.message(response_message)

    # Generate a unique message ID and add it to the SMS data
    message_id = str(uuid4())
    sms_data["wbor_message_id"] = message_id
    set_ack_event(message_id)

    logger.debug("Attempting to fetch caller name for SMS message")

    def fetch_sender(sms_data, timeout=3):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(fetch_name, sms_data)
            try:
                return future.result(timeout=timeout)
            except FuturesTimeoutError:
                logger.warning("Timeout occurred while fetching sender name")
                return "Unknown"

    try:
        sender_name = fetch_sender(sms_data)
    except (TwilioRestException, FuturesTimeoutError) as e:
        logger.error("Error fetching sender name: `%s`", str(e))
        sender_name = "Unknown"
    sms_data["SenderName"] = sender_name

    sms_data["source"] = SOURCE

    # `sms_data` now includes original Twilio content, `SenderName`, `source`, and `wbor_message_id`
    Thread(target=publish_to_exchange, args=(SOURCE, "sms.incoming", sms_data)).start()
    # TODO: if the sender is banned, append `.banned` to the routing key, that way downstream
    # consumers can subscribe to only non-banned messages if desired (e.g. wbor-studio-dashboard)
    # The alternative is setting a header value? Such as `x-banned: true`?

    # Wait for acknowledgment from the GroupMe consumer so that fallback handler can be
    # triggered if the message fails to process for any reason
    logger.debug("Waiting for acknowledgment for message_id: `%s`", message_id)

    # Note: this requires more than one worker process to work properly
    # (since the main thread is blocked waiting for the /acknowledgment)
    start_time = datetime.now()
    while (datetime.now() - start_time).seconds < REDIS_ACK_EXPIRATION_S:
        ack_status = redis_client.get(message_id)
        if not ack_status:  # ACK received (deleted by /acknowledge endpoint)
            # So if it's not found, the message was processed
            # Return an empty TwiML response to acknowledge receipt of the message
            logger.debug("Acknowledgment received: `%s`", message_id)
            return str(resp)
    logger.error(
        "Timeout met while waiting for acknowledgment for message_id: `%s`", message_id
    )
    delete_ack_event(message_id)
    return "Failed to process message", 500


@app.route("/send", methods=["GET"])
def browser_queue_outgoing_sms() -> str:
    """
    Send an SMS message using the Twilio API from a browser address bar.
    Requires a password.

    Parameters:
    - recipient_number (str): The phone number to send the message to.
    - body (str): The body of the SMS message.
    - password (str): The password for authorization.

    Does not accept international numbers (e.g. +44).

    Expects `recipient_number` to be in E.164 format, e.g. +12077253250.
    Encoding the `+` as %2B also works.
    """
    logger.info("Received request to send SMS from browser...")
    # Don't let strangers send messages as if they were us!
    password = request.args.get("password")
    if password != APP_PASSWORD:
        # TODO: fix this to log the IP address from outside the container
        # logger.warning("Unauthorized access attempt from IP: %s", request.remote_addr)
        logger.warning("Unauthorized access attempt")
        abort(403, "Unauthorized access")

    recipient_number = request.args.get("recipient_number", "").replace(" ", "+")
    if not recipient_number or not re.fullmatch(r"^\+?\d{10,15}$", recipient_number):
        if not recipient_number:
            logger.warning("Recipient number missing")
            abort(400, "Recipient missing")

        if not recipient_number.startswith("+1"):
            logger.warning(
                "Invalid recipient number format (recipient number must start with +1): `%s`",
                recipient_number,
            )
            abort(400, "Recipient number must start with +1")
        logger.warning("Invalid recipient number format: `%s`", recipient_number)
        abort(400, "Invalid recipient number format (must use the E.164 standard)")
    if recipient_number == TWILIO_PHONE_NUMBER:
        logger.warning(
            "Recipient number must not be the same as the sending number: `%s`",
            recipient_number,
        )
        abort(400, "Recipient number must not be the same as the sending number")

    message = request.args.get("body")
    if not message:
        logger.warning("Message body content missing")
        abort(400, "Message body text is required")
    if len(message) > TWILIO_CHARACTER_LIMIT:
        logger.warning("Message too long: `%d` characters", len(message))
        abort(400, "Message exceeds character limit")

    # Queue the message for sending
    message_id = str(uuid4())  # Generate a unique ID for tracking
    outgoing_message = {
        "wbor_message_id": message_id,
        "recipient_number": recipient_number,
        "body": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    publish_to_exchange(SOURCE, "sms.outgoing", outgoing_message)
    logger.info("Message queued for sending. UID: `%s`", message_id)
    return f"Message queued for sending to {recipient_number}"


@app.route("/ban", methods=["GET"])
def browser_ban_contact() -> str:
    """
    Ban a phone number from all future communication.

    Parameters:
    - number (str): The phone number to ban.
    - password (str): The password for authorization.

    Does not accept international numbers (e.g. +44).

    Expects `number` to be in E.164 format, e.g. +12077253250.
    Encoding the `+` as %2B also works.
    """
    logger.info("Received request to ban a number from browser...")
    # Don't let strangers ban as if they were us!
    password = request.args.get("password")
    if password != APP_PASSWORD:
        # TODO: fix this to log the IP address from outside the container
        # logger.warning("Unauthorized access attempt from IP: %s", request.remote_addr)
        logger.warning("Unauthorized access attempt")
        abort(403, "Unauthorized access")

    ban_number = request.args.get("number", "").replace(" ", "+")
    message = request.args.get("body")

    if not ban_number or not re.fullmatch(r"^\+?\d{10,15}$", ban_number):
        if not ban_number:
            logger.warning("Ban number missing")
            abort(400, "Ban number missing")

        if not ban_number.startswith("+1"):
            logger.warning(
                "Invalid ban number format (ban number must start with +1): `%s`",
                ban_number,
            )
            abort(400, "Ban number must start with +1")
        logger.warning("Invalid ban number format: `%s`", ban_number)
        abort(400, "Invalid ban number format (must use the E.164 standard)")

    # Queue the message for sending
    message_id = str(uuid4())  # Generate a unique ID for tracking
    body = {
        "wbor_message_id": message_id,
        "ban_number": ban_number,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    publish_to_exchange(SOURCE, "ban", body)
    logger.info("Message queued for sending. UID: `%s`", message_id)
    return f"Ban queued for {ban_number}"


@app.route("/unban", methods=["GET"])
def browser_unban_contact() -> str:
    """
    Remove a phone number from the ban list.

    Parameters:
    - number (str): The phone number to unban.
    - password (str): The password for authorization.

    Does not accept international numbers (e.g. +44).

    Expects `number` to be in E.164 format, e.g. +12077253250.
    Encoding the `+` as %2B also works.
    """
    logger.info("Received request to unban a number from browser...")
    # Don't let strangers ban as if they were us!
    password = request.args.get("password")
    if password != APP_PASSWORD:
        # TODO: fix this to log the IP address from outside the container
        # logger.warning("Unauthorized access attempt from IP: %s", request.remote_addr)
        logger.warning("Unauthorized access attempt")
        abort(403, "Unauthorized access")

    unban_number = request.args.get("number", "").replace(" ", "+")
    message = request.args.get("body")

    if not unban_number or not re.fullmatch(r"^\+?\d{10,15}$", unban_number):
        if not unban_number:
            logger.warning("Unbn number missing")
            abort(400, "Unban number missing")

        if not unban_number.startswith("+1"):
            logger.warning(
                "Invalid unban number format (unban number must start with +1): `%s`",
                unban_number,
            )
            abort(400, "Unban number must start with +1")
        logger.warning("Invalid unban number format: `%s`", unban_number)
        abort(400, "Invalid unban number format (must use the E.164 standard)")

    # Queue the message for sending
    message_id = str(uuid4())  # Generate a unique ID for tracking
    body = {
        "wbor_message_id": message_id,
        "unban_number": unban_number,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    publish_to_exchange(SOURCE, "unban", body)
    logger.info("Message queued for sending. UID: `%s`", message_id)
    return f"Unban queued for {unban_number}"


@app.route("/voice-intelligence", methods=["POST"])
def log_webhook() -> str:
    """
    Endpoint for receiving Voice Intelligence webhook events.

    Expects a JSON payload with a 'transcript_sid' field.

    Returns:
    - str: A 202 Accepted response.
    """
    data = request.get_json()
    logger.info("Received Voice Intelligence webhook fire with data: `%s`", data)
    logger.info("Transcript SID: `%s`", data.get("transcript_sid"))
    publish_to_exchange(SOURCE, "voice-intelligence", data)
    return "Accepted", 202


@app.route("/call-events", methods=["POST"])
def log_call_event() -> str:
    """
    Endpoint for receiving Call Event webhook events.

    Returns:
    - str: A 202 Accepted response.
    """
    data = request.form.to_dict()
    logger.info("Received Call Event webhook fire with data: `%s`", data)
    publish_to_exchange(SOURCE, "call-events", data)
    return "Accepted", 202


@app.route("/")
def is_online() -> str:
    """
    Health check endpoint.
    """
    return "OK", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=APP_PORT)
