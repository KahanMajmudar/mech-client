# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2024 Valory AG
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""This script allows sending a Request to an on-chain mech marketplace and waiting for the Deliver."""


import asyncio
import json
import sys
import time
from dataclasses import asdict, make_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

import requests
import websocket
from aea.crypto.base import Crypto
from aea_ledger_ethereum import EthereumApi, EthereumCrypto
from eth_utils import to_checksum_address
from web3.constants import ADDRESS_ZERO
from web3.contract import Contract as Web3Contract

from mech_client.fetch_ipfs_hash import fetch_ipfs_hash
from mech_client.interact import (
    ConfirmationType,
    MAX_RETRIES,
    MechMarketplaceRequestConfig,
    PRIVATE_KEY_FILE_PATH,
    TIMEOUT,
    WAIT_SLEEP,
    calculate_topic_id,
    get_contract,
    get_mech_config,
    verify_or_retrieve_tool,
)
from mech_client.prompt_to_ipfs import push_metadata_to_ipfs
from mech_client.wss import (
    register_event_handlers,
    wait_for_receipt,
    watch_for_marketplace_data_url_from_wss,
    watch_for_marketplace_request_id,
)


# false positives for [B105:hardcoded_password_string] Possible hardcoded password
PAYMENT_TYPE_NATIVE = (
    "ba699a34be8fe0e7725e93dcbce1701b0211a8ca61330aaeb8a05bf2ec7abed1"  # nosec
)
PAYMENT_TYPE_TOKEN = (
    "3679d66ef546e66ce9057c4a052f317b135bc8e8c509638f7966edfd4fcf45e9"  # nosec
)
PAYMENT_TYPE_NVM = (
    "803dd08fe79d91027fc9024e254a0942372b92f3ccabc1bd19f4a5c2b251c316"  # nosec
)

CHAIN_TO_WRAPPED_TOKEN = {
    1: "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    10: "0x4200000000000000000000000000000000000006",
    100: "0xe91D153E0b41518A2Ce8Dd3D7944Fa863463a97d",
    137: "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
    8453: "0x4200000000000000000000000000000000000006",
    42220: "0x471EcE3750Da237f93B8E339c536989b8978a438",
}


CHAIN_TO_DEFAULT_MECH_MARKETPLACE_REQUEST_CONFIG = {
    100: {
        "mech_marketplace_contract": "0x735FAAb1c4Ec41128c367AFb5c3baC73509f70bB",
        "priority_mech_address": "0x478ad20eD958dCC5AD4ABa6F4E4cc51e07a840E4",
        "response_timeout": 300,
        "payment_data": "0x",
    }
}


def get_event_signatures(abi: List) -> Tuple[str, str]:
    """Calculate `Marketplace Request` and `Marketplace Deliver` event topics"""
    marketplace_request, marketplace_deliver = "", ""
    for obj in abi:
        if obj["type"] != "event":
            continue
        if obj["name"] == "MarketplaceDeliver":
            marketplace_deliver = calculate_topic_id(event=obj)
        if obj["name"] == "MarketplaceRequest":
            marketplace_request = calculate_topic_id(event=obj)
    return marketplace_request, marketplace_deliver


def fetch_mech_info(
    ledger_api: EthereumApi,
    mech_marketplace_contract: Web3Contract,
    priority_mech_address: str,
) -> Tuple[str, int, int, str, Web3Contract]:
    """
    Fetchs the info of the requested mech.

    :param ledger_api: The Ethereum API used for interacting with the ledger.
    :type ledger_api: EthereumApi
    :param mech_marketplace_contract: The mech marketplace contract instance.
    :type mech_marketplace_contract: Web3Contract
    :param priority_mech_address: Requested mech address
    :type priority_mech_address: str
    :return: The mech info containing payment_type, service_id, max_delivery_rate, mech_payment_balance_tracker and Mech contract.
    :rtype: Tuple[str, int, int, str, Contract]
    """

    with open(Path(__file__).parent / "abis" / "IMech.json", encoding="utf-8") as f:
        abi = json.load(f)

    mech_contract = get_contract(
        contract_address=priority_mech_address, abi=abi, ledger_api=ledger_api
    )
    payment_type_bytes = mech_contract.functions.paymentType().call()
    max_delivery_rate = mech_contract.functions.maxDeliveryRate().call()
    service_id = mech_contract.functions.serviceId().call()
    payment_type = payment_type_bytes.hex()

    mech_payment_balance_tracker = (
        mech_marketplace_contract.functions.mapPaymentTypeBalanceTrackers(
            payment_type_bytes
        ).call()
    )

    if payment_type not in [PAYMENT_TYPE_NATIVE, PAYMENT_TYPE_TOKEN, PAYMENT_TYPE_NVM]:
        print("  - Invalid mech type detected.")
        sys.exit(1)

    return (
        payment_type,
        service_id,
        max_delivery_rate,
        mech_payment_balance_tracker,
        mech_contract,
    )


def approve_price_tokens(
    crypto: EthereumCrypto,
    ledger_api: EthereumApi,
    wrapped_token: str,
    mech_payment_balance_tracker: str,
    price: int,
) -> str:
    """
    Sends the approve tx for wrapped token of the sender to the requested mech's balance payment tracker contract.

    :param crypto: The Ethereum crypto object.
    :type crypto: EthereumCrypto
    :param ledger_api: The Ethereum API used for interacting with the ledger.
    :type ledger_api: EthereumApi
    :param wrapped_token: The wrapped token contract address.
    :type wrapped_token: str
    :param mech_payment_balance_tracker: Requested mech's balance tracker contract address
    :type mech_payment_balance_tracker: str
    :param price: Amount of wrapped_token to approve
    :type price: int
    :return: The transaction digest.
    :rtype: str
    """
    sender = crypto.address

    with open(Path(__file__).parent / "abis" / "IToken.json", encoding="utf-8") as f:
        abi = json.load(f)

    token_contract = get_contract(
        contract_address=wrapped_token, abi=abi, ledger_api=ledger_api
    )

    user_token_balance = token_contract.functions.balanceOf(sender).call()
    if user_token_balance < price:
        print(
            f"  - Sender Token balance low. Needed: {price}, Actual: {user_token_balance}"
        )
        print(f"  - Sender Address: {sender}")
        sys.exit(1)

    tx_args = {"sender_address": sender, "value": 0, "gas": 60000}
    raw_transaction = ledger_api.build_transaction(
        contract_instance=token_contract,
        method_name="approve",
        method_args={"_to": mech_payment_balance_tracker, "_value": price},
        tx_args=tx_args,
        raise_on_try=True,
    )
    signed_transaction = crypto.sign_transaction(raw_transaction)
    transaction_digest = ledger_api.send_signed_transaction(
        signed_transaction,
        raise_on_try=True,
    )
    return transaction_digest


def fetch_requester_nvm_subscription_balance(
    requester: str,
    ledger_api: EthereumApi,
    mech_payment_balance_tracker: str,
) -> int:
    """
    Fetches the requester nvm subscription balance.

    :param requester: The requester's address.
    :type requester: str
    :param ledger_api: The Ethereum API used for interacting with the ledger.
    :type ledger_api: EthereumApi
    :param mech_payment_balance_tracker: Requested mech's balance tracker contract address
    :type mech_payment_balance_tracker: str
    :return: The requester balance.
    :rtype: int
    """
    with open(
        Path(__file__).parent / "abis" / "BalanceTrackerNvmSubscriptionNative.json",
        encoding="utf-8",
    ) as f:
        abi = json.load(f)

    nvm_balance_tracker_contract = get_contract(
        contract_address=mech_payment_balance_tracker, abi=abi, ledger_api=ledger_api
    )
    subscription_nft_address = (
        nvm_balance_tracker_contract.functions.subscriptionNFT().call()
    )
    subscription_id = (
        nvm_balance_tracker_contract.functions.subscriptionTokenId().call()
    )

    with open(
        Path(__file__).parent / "abis" / "IERC1155.json",
        encoding="utf-8",
    ) as f:
        abi = json.load(f)

    subscription_nft_contract = get_contract(
        contract_address=subscription_nft_address, abi=abi, ledger_api=ledger_api
    )
    requester_balance = subscription_nft_contract.functions.balanceOf(
        requester, subscription_id
    ).call()

    return requester_balance


def send_marketplace_request(  # pylint: disable=too-many-arguments,too-many-locals
    crypto: EthereumCrypto,
    ledger_api: EthereumApi,
    marketplace_contract: Web3Contract,
    gas_limit: int,
    prompt: str,
    tool: str,
    method_args_data: MechMarketplaceRequestConfig,
    extra_attributes: Optional[Dict[str, Any]] = None,
    price: int = 10_000_000_000_000_000,
    retries: Optional[int] = None,
    timeout: Optional[float] = None,
    sleep: Optional[float] = None,
) -> Optional[str]:
    """
    Sends a request to the mech.

    :param crypto: The Ethereum crypto object.
    :type crypto: EthereumCrypto
    :param ledger_api: The Ethereum API used for interacting with the ledger.
    :type ledger_api: EthereumApi
    :param marketplace_contract: The mech marketplace contract instance.
    :type marketplace_contract: Web3Contract
    :param gas_limit: Gas limit.
    :type gas_limit: int
    :param prompt: The request prompt.
    :type prompt: str
    :param tool: The requested tool.
    :type tool: str
    :param method_args_data: Method data to use to call the marketplace contract request
    :type method_args_data: MechMarketplaceRequestConfig
    :param extra_attributes: Extra attributes to be included in the request metadata.
    :type extra_attributes: Optional[Dict[str,Any]]
    :param price: The price for the request (default: 10_000_000_000_000_000).
    :type price: int
    :param retries: Number of retries for sending a transaction
    :type retries: int
    :param timeout: Timeout to wait for the transaction
    :type timeout: float
    :param sleep: Amount of sleep before retrying the transaction
    :type sleep: float
    :return: The transaction hash.
    :rtype: Optional[str]
    """
    v1_file_hash_hex_truncated, v1_file_hash_hex = push_metadata_to_ipfs(
        prompt, tool, extra_attributes
    )
    print(
        f"  - Prompt uploaded: https://gateway.autonolas.tech/ipfs/{v1_file_hash_hex}"
    )
    method_name = "request"
    method_args = {
        "requestData": v1_file_hash_hex_truncated,
        "maxDeliveryRate": method_args_data.delivery_rate,
        "paymentType": "0x" + cast(str, method_args_data.payment_type),
        "priorityMech": to_checksum_address(method_args_data.priority_mech_address),
        "responseTimeout": method_args_data.response_timeout,
        "paymentData": method_args_data.payment_data,
    }
    tx_args = {
        "sender_address": crypto.address,
        "value": price,
        "gas": gas_limit,
    }

    tries = 0
    retries = retries or MAX_RETRIES
    timeout = timeout or TIMEOUT
    sleep = sleep or WAIT_SLEEP
    deadline = datetime.now().timestamp() + timeout

    while tries < retries and datetime.now().timestamp() < deadline:
        tries += 1
        try:
            raw_transaction = ledger_api.build_transaction(
                contract_instance=marketplace_contract,
                method_name=method_name,
                method_args=method_args,
                tx_args=tx_args,
                raise_on_try=True,
            )
            signed_transaction = crypto.sign_transaction(raw_transaction)
            transaction_digest = ledger_api.send_signed_transaction(
                signed_transaction,
                raise_on_try=True,
            )
            return transaction_digest
        except Exception as e:  # pylint: disable=broad-except
            print(
                f"Error occured while sending the transaction: {e}; Retrying in {sleep}"
            )
            time.sleep(sleep)
    return None


def send_offchain_marketplace_request(  # pylint: disable=too-many-arguments,too-many-locals
    crypto: EthereumCrypto,
    marketplace_contract: Web3Contract,
    prompt: str,
    tool: str,
    method_args_data: MechMarketplaceRequestConfig,
    extra_attributes: Optional[Dict[str, Any]] = None,
    retries: Optional[int] = None,
    timeout: Optional[float] = None,
    sleep: Optional[float] = None,
) -> Optional[Dict]:
    """
    Sends an offchain request to the mech.

    :param crypto: The Ethereum crypto object.
    :type crypto: EthereumCrypto
    :param marketplace_contract: The mech marketplace contract instance.
    :type marketplace_contract: Web3Contract
    :param prompt: The request prompt.
    :type prompt: str
    :param tool: The requested tool.
    :type tool: str
    :param method_args_data: Method data to use to call the marketplace contract request
    :type method_args_data: MechMarketplaceRequestConfig
    :param extra_attributes: Extra attributes to be included in the request metadata.
    :type extra_attributes: Optional[Dict[str,Any]]
    :param retries: Number of retries for sending a transaction
    :type retries: int
    :param timeout: Timeout to wait for the transaction
    :type timeout: float
    :param sleep: Amount of sleep before retrying the transaction
    :type sleep: float
    :return: The dict containing request info.
    :rtype: Optional[Dict]
    """
    v1_file_hash_hex_truncated, v1_file_hash_hex, ipfs_data = fetch_ipfs_hash(
        prompt, tool, extra_attributes
    )
    print(
        f"  - Prompt will shortly be uploaded to: https://gateway.autonolas.tech/ipfs/{v1_file_hash_hex}"
    )
    method_args = {
        "requestData": v1_file_hash_hex_truncated,
        "maxDeliveryRate": method_args_data.delivery_rate,
        "paymentType": "0x" + cast(str, method_args_data.payment_type),
        "priorityMech": to_checksum_address(method_args_data.priority_mech_address),
        "responseTimeout": method_args_data.response_timeout,
        "paymentData": method_args_data.payment_data,
    }

    tries = 0
    retries = retries or MAX_RETRIES
    timeout = timeout or TIMEOUT
    sleep = sleep or WAIT_SLEEP
    deadline = datetime.now().timestamp() + timeout

    while tries < retries and datetime.now().timestamp() < deadline:
        tries += 1
        try:
            nonce = marketplace_contract.functions.mapNonces(crypto.address).call()
            delivery_rate = method_args["maxDeliveryRate"]
            request_id = marketplace_contract.functions.getRequestId(
                method_args["priorityMech"],
                crypto.address,
                method_args["requestData"],
                method_args["maxDeliveryRate"],
                method_args["paymentType"],
                nonce,
            ).call()
            request_id_int = int.from_bytes(request_id, byteorder="big")
            signature = crypto.sign_message(request_id, is_deprecated_mode=True)

            payload = {
                "sender": crypto.address,
                "signature": signature,
                "ipfs_hash": v1_file_hash_hex_truncated,
                "request_id": request_id_int,
                "delivery_rate": delivery_rate,
                "nonce": nonce,
                "ipfs_data": ipfs_data,
            }
            # @todo changed hardcoded url
            response = requests.post(
                "http://localhost:8000/send_signed_requests",
                data=payload,
                headers={"Content-Type": "application/json"},
            ).json()
            return response

        except Exception as e:  # pylint: disable=broad-except
            print(
                f"Error occured while sending the offchain request: {e}; Retrying in {sleep}"
            )
            time.sleep(sleep)
    return None


def wait_for_marketplace_data_url(  # pylint: disable=too-many-arguments, unused-argument
    request_id: str,
    wss: websocket.WebSocket,
    mech_contract: Web3Contract,
    subgraph_url: str,
    deliver_signature: str,
    ledger_api: EthereumApi,
    crypto: Crypto,
    confirmation_type: ConfirmationType = ConfirmationType.WAIT_FOR_BOTH,
) -> Any:
    """
    Wait for data from on-chain/off-chain.

    :param request_id: The ID of the request.
    :type request_id: str
    :param wss: The WebSocket connection object.
    :type wss: websocket.WebSocket
    :param mech_contract: The mech contract instance.
    :type mech_contract: Web3Contract
    :param subgraph_url: Subgraph URL.
    :type subgraph_url: str
    :param deliver_signature: Topic signature for MarketplaceDeliver event
    :type deliver_signature: str
    :param ledger_api: The Ethereum API used for interacting with the ledger.
    :type ledger_api: EthereumApi
    :param crypto: The cryptographic object.
    :type crypto: Crypto
    :param confirmation_type: The confirmation type for the interaction (default: ConfirmationType.WAIT_FOR_BOTH).
    :type confirmation_type: ConfirmationType
    :return: The data received from on-chain/off-chain.
    :rtype: Any
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    tasks = []

    if confirmation_type in (
        ConfirmationType.OFF_CHAIN,
        ConfirmationType.WAIT_FOR_BOTH,
    ):
        print("Off chain to be implemented")

    if confirmation_type in (
        ConfirmationType.ON_CHAIN,
        ConfirmationType.WAIT_FOR_BOTH,
    ):
        on_chain_task = loop.create_task(
            watch_for_marketplace_data_url_from_wss(
                request_id=request_id,
                wss=wss,
                mech_contract=mech_contract,
                deliver_signature=deliver_signature,
                ledger_api=ledger_api,
                loop=loop,
            )
        )
        tasks.append(on_chain_task)

        if subgraph_url:
            print("Subgraph to be implemented")

    async def _wait_for_tasks() -> Any:  # type: ignore
        """Wait for tasks to finish."""
        (finished, *_), unfinished = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.wait(unfinished)
        return finished.result()

    result = loop.run_until_complete(_wait_for_tasks())
    return result


def wait_for_offchain_marketplace_data(request_id: str) -> Any:
    """
    Watches for data off-chain on mech.

    :param request_id: The ID of the request.
    :type request_id: str
    :return: The data returned by the mech.
    :rtype: Any
    """
    while True:
        try:
            # @todo change hardcoded url
            response = requests.get(
                "http://localhost:8000/fetch_offchain_info",
                data={"request_id": request_id},
            ).json()
            if response:
                return response
        except Exception:  # pylint: disable=broad-except
            time.sleep(1)


def check_prepaid_balances(
    crypto: Crypto,
    ledger_api: EthereumApi,
    mech_payment_balance_tracker: str,
    payment_type: str,
    max_delivery_rate: int,
) -> None:
    """
    Checks the requester's prepaid balances for native and token mech.

    :param crypto: The cryptographic object.
    :type crypto: Crypto
    :param ledger_api: The Ethereum API used for interacting with the ledger.
    :type ledger_api: EthereumApi
    :param mech_payment_balance_tracker: The mech's balance tracker contract address.
    :type mech_payment_balance_tracker: str
    :param payment_type: The payment type of the mech.
    :type payment_type: str
    :param max_delivery_rate: The max_delivery_rate of the mech
    :type max_delivery_rate: int
    """
    requester = crypto.address
    if payment_type == PAYMENT_TYPE_NATIVE:
        with open(
            Path(__file__).parent / "abis" / "BalanceTrackerFixedPriceNative.json",
            encoding="utf-8",
        ) as f:
            abi = json.load(f)

        balance_tracker_contract = get_contract(
            contract_address=mech_payment_balance_tracker,
            abi=abi,
            ledger_api=ledger_api,
        )
        requester_balance = balance_tracker_contract.functions.mapRequesterBalances(
            requester
        ).call()
        if requester_balance < max_delivery_rate:
            print(
                f"  - Sender Native deposited balance low. Needed: {max_delivery_rate}, Actual: {requester_balance}"
            )
            print(f"  - Sender Address: {requester}")
            print("  - Please use scripts/deposit_native.py to add balance")
            sys.exit(1)

    if payment_type == PAYMENT_TYPE_TOKEN:
        with open(
            Path(__file__).parent / "abis" / "BalanceTrackerFixedPriceToken.json",
            encoding="utf-8",
        ) as f:
            abi = json.load(f)

        balance_tracker_contract = get_contract(
            contract_address=mech_payment_balance_tracker,
            abi=abi,
            ledger_api=ledger_api,
        )
        requester_balance = balance_tracker_contract.functions.mapRequesterBalances(
            requester
        ).call()
        if requester_balance < max_delivery_rate:
            print(
                f"  - Sender Token deposited balance low. Needed: {max_delivery_rate}, Actual: {requester_balance}"
            )
            print(f"  - Sender Address: {requester}")
            print("  - Please use scripts/deposit_token.py to add balance")
            sys.exit(1)


def marketplace_interact(  # pylint: disable=too-many-arguments, too-many-locals, too-many-statements, too-many-return-statements
    prompt: str,
    priority_mech: str,
    use_prepaid: bool = False,
    use_offchain: bool = False,
    tool: Optional[str] = None,
    extra_attributes: Optional[Dict[str, Any]] = None,
    private_key_path: Optional[str] = None,
    confirmation_type: ConfirmationType = ConfirmationType.WAIT_FOR_BOTH,
    retries: Optional[int] = None,
    timeout: Optional[float] = None,
    sleep: Optional[float] = None,
    chain_config: Optional[str] = None,
) -> Any:
    """
    Interact with mech marketplace contract.

    :param prompt: The interaction prompt.
    :type prompt: str
    :param use_prepaid: Whether to use prepaid model or not.
    :type use_prepaid: bool
    :param use_offchain: Whether to use offchain model or not.
    :type use_offchain: bool
    :param tool: The tool to interact with (optional).
    :type tool: Optional[str]
    :param extra_attributes: Extra attributes to be included in the request metadata (optional).
    :type extra_attributes: Optional[Dict[str, Any]]
    :param private_key_path: The path to the private key file (optional).
    :type private_key_path: Optional[str]
    :param confirmation_type: The confirmation type for the interaction (default: ConfirmationType.WAIT_FOR_BOTH).
    :type confirmation_type: ConfirmationType
    :return: The data received from on-chain/off-chain.
    :param retries: Number of retries for sending a transaction
    :type retries: int
    :param timeout: Timeout to wait for the transaction
    :type timeout: float
    :param sleep: Amount of sleep before retrying the transaction
    :type sleep: float
    :param chain_config: Id of the mech's chain configuration (stored configs/mechs.json)
    :type chain_config: str:
    :rtype: Any
    """

    mech_config = get_mech_config(chain_config)
    ledger_config = mech_config.ledger_config
    priority_mech_address = priority_mech
    mech_marketplace_contract = mech_config.mech_marketplace_contract
    chain_id = ledger_config.chain_id

    if mech_marketplace_contract == ADDRESS_ZERO:
        print(f"Mech Marketplace not yet supported on {chain_config}")
        return None

    config_values = CHAIN_TO_DEFAULT_MECH_MARKETPLACE_REQUEST_CONFIG[chain_id].copy()
    if priority_mech_address is not None:
        print("Custom Mech detected")
        config_values.update(
            {
                "priority_mech_address": priority_mech_address,
                "mech_marketplace_contract": mech_marketplace_contract,
            }
        )

    mech_marketplace_request_config: MechMarketplaceRequestConfig = make_dataclass(
        "MechMarketplaceRequestConfig",
        ((k, type(v)) for k, v in config_values.items()),
    )(**config_values)

    contract_address = cast(
        str, mech_marketplace_request_config.mech_marketplace_contract
    )

    private_key_path = private_key_path or PRIVATE_KEY_FILE_PATH
    if not Path(private_key_path).exists():
        raise FileNotFoundError(
            f"Private key file `{private_key_path}` does not exist!"
        )

    wss = websocket.create_connection(mech_config.wss_endpoint)
    crypto = EthereumCrypto(private_key_path=private_key_path)
    ledger_api = EthereumApi(**asdict(ledger_config))

    with open(
        Path(__file__).parent / "abis" / "MechMarketplace.json", encoding="utf-8"
    ) as f:
        abi = json.load(f)

    mech_marketplace_contract = get_contract(
        contract_address=contract_address, abi=abi, ledger_api=ledger_api
    )

    print("Fetching Mech Info...")
    priority_mech_address = cast(
        str, mech_marketplace_request_config.priority_mech_address
    )
    (
        payment_type,
        service_id,
        max_delivery_rate,
        mech_payment_balance_tracker,
        mech_contract,
    ) = fetch_mech_info(
        ledger_api,
        mech_marketplace_contract,
        priority_mech_address,
    )
    mech_marketplace_request_config.delivery_rate = max_delivery_rate
    mech_marketplace_request_config.payment_type = payment_type

    # Expected parameters: agent id and agent registry contract address
    # Note: passing service id and service registry contract address as internal function calls are same
    tool = verify_or_retrieve_tool(
        agent_id=cast(int, service_id),
        ledger_api=ledger_api,
        tool=tool,
        agent_registry_contract=mech_config.service_registry_contract,
        contract_abi_url=mech_config.contract_abi_url,
    )

    (
        marketplace_request_event_signature,
        marketplace_deliver_event_signature,
    ) = get_event_signatures(abi=abi)

    register_event_handlers(
        wss=wss,
        contract_address=contract_address,
        crypto=crypto,
        request_signature=marketplace_request_event_signature,
        deliver_signature=marketplace_deliver_event_signature,
    )

    if not use_prepaid:
        price = max_delivery_rate
        if payment_type == PAYMENT_TYPE_TOKEN:
            print("Token Mech detected, approving wrapped token for price payment...")
            wxdai = CHAIN_TO_WRAPPED_TOKEN[chain_id]
            approve_tx = approve_price_tokens(
                crypto, ledger_api, wxdai, mech_payment_balance_tracker, price
            )
            if not approve_tx:
                print("Unable to approve allowance")
                return None

            transaction_url_formatted = mech_config.transaction_url.format(
                transaction_digest=approve_tx
            )
            print(f"  - Transaction sent: {transaction_url_formatted}")
            print("  - Waiting for transaction receipt...")
            wait_for_receipt(approve_tx, ledger_api)
            # set price 0 to not send any msg.value in request transaction for token type mech
            price = 0

    else:
        print("Prepaid request to be used, skipping payment")
        price = 0

        check_prepaid_balances(
            crypto,
            ledger_api,
            mech_payment_balance_tracker,
            payment_type,
            max_delivery_rate,
        )

    if payment_type == PAYMENT_TYPE_NVM:
        print("Nevermined Mech detected, subscription credits to be used")
        requester = crypto.address
        requester_balance = fetch_requester_nvm_subscription_balance(
            requester, ledger_api, mech_payment_balance_tracker
        )
        if requester_balance < price:
            print(
                f"  - Sender Subscription balance low. Needed: {price}, Actual: {requester_balance}"
            )
            print(f"  - Sender Address: {requester}")
            sys.exit(1)

        # set price 0 to not send any msg.value in request transaction for nvm type mech
        price = 0

    if not use_offchain:
        print("Sending Mech Marketplace request...")
        transaction_digest = send_marketplace_request(
            crypto=crypto,
            ledger_api=ledger_api,
            marketplace_contract=mech_marketplace_contract,
            gas_limit=mech_config.gas_limit,
            price=price,
            prompt=prompt,
            tool=tool,
            method_args_data=mech_marketplace_request_config,
            extra_attributes=extra_attributes,
            retries=retries,
            timeout=timeout,
            sleep=sleep,
        )

        if not transaction_digest:
            print("Unable to send request")
            return None

        transaction_url_formatted = mech_config.transaction_url.format(
            transaction_digest=transaction_digest
        )
        print(f"  - Transaction sent: {transaction_url_formatted}")
        print("  - Waiting for transaction receipt...")

        request_id = watch_for_marketplace_request_id(
            marketplace_contract=mech_marketplace_contract,
            ledger_api=ledger_api,
            tx_hash=transaction_digest,
        )
        request_id_int = int.from_bytes(bytes.fromhex(request_id), byteorder="big")
        print(f"  - Created on-chain request with ID {request_id_int}")
        print("")

        data_url = wait_for_marketplace_data_url(
            request_id=request_id,
            wss=wss,
            mech_contract=mech_contract,
            subgraph_url=mech_config.subgraph_url,
            deliver_signature=marketplace_deliver_event_signature,
            ledger_api=ledger_api,
            crypto=crypto,
            confirmation_type=confirmation_type,
        )

        if data_url:
            print(f"  - Data arrived: {data_url}")
            data = requests.get(f"{data_url}/{request_id_int}", timeout=30).json()
            print("  - Data from agent:")
            print(json.dumps(data, indent=2))
            return data
        return None

    print("Sending Offchain Mech Marketplace request...")
    response = send_offchain_marketplace_request(
        crypto=crypto,
        marketplace_contract=mech_marketplace_contract,
        prompt=prompt,
        tool=tool,
        method_args_data=mech_marketplace_request_config,
        extra_attributes=extra_attributes,
        retries=retries,
        timeout=timeout,
        sleep=sleep,
    )

    if not response:
        return None

    request_id = response["request_id"]
    print(f"  - Created off-chain request with ID {request_id}")
    print("")

    # @note as we are directly querying data from done task list, we get the full data instead of the ipfs hash
    print("Waiting for Offchain Mech Marketplace deliver...")
    data = wait_for_offchain_marketplace_data(
        request_id=request_id,
    )

    if data:
        task_result = data["task_result"]
        data_url = f"https://gateway.autonolas.tech/ipfs/f01701220{task_result}"
        print(f"  - Data arrived: {data_url}")
        data = requests.get(f"{data_url}/{request_id}", timeout=30).json()
        print("  - Data from agent:")
        print(json.dumps(data, indent=2))
        return data
    return None
