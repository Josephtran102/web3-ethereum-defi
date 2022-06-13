"""Uniswap v3 events.

Currently we are tracking these events:
    - PoolCreated
    - Swap
    - Mint
    - Burn
"""
import csv
import datetime
from pathlib import Path

from tqdm import tqdm
from web3 import Web3

from eth_defi.abi import get_contract
from eth_defi.event_reader.conversion import (
    convert_uint256_bytes_to_address,
    convert_uint256_bytes_to_int,
    convert_uint256_string_to_address,
    convert_uint256_string_to_int,
    decode_data,
)
from eth_defi.event_reader.logresult import LogContext
from eth_defi.event_reader.reader import LogResult, read_events_concurrent
from eth_defi.event_reader.web3factory import TunedWeb3Factory
from eth_defi.event_reader.web3worker import create_thread_pool_executor
from eth_defi.token import TokenDetails, fetch_erc20_details
from eth_defi.uniswap_v3.constants import UNISWAP_V3_FACTORY_CREATED_AT_BLOCK


class TokenCache(LogContext):
    """Manage cache of token data when doing PoolCreated look-up.

    Do not do extra requests for already known tokens.
    """

    def __init__(self):
        self.cache = {}

    def get_token_info(self, web3: Web3, address: str) -> TokenDetails:
        if address not in self.cache:
            self.cache[address] = fetch_erc20_details(web3, address, raise_on_error=False)
        return self.cache[address]


def _decode_base(log: LogResult) -> dict:
    block_time = datetime.datetime.utcfromtimestamp(log["timestamp"])

    return {
        "block_number": int(log["blockNumber"], 16),
        "timestamp": block_time.isoformat(),
        "tx_hash": log["transactionHash"],
        "log_index": int(log["logIndex"], 16),
    }


def decode_pool_created(log: LogResult) -> dict:
    """Process a pool created event. The event signature is:

    .. code-block::

        event PoolCreated(
            address indexed token0,
            address indexed token1,
            uint24 indexed fee,
            int24 tickSpacing,
            address pool
        );
    """
    # Do additional lookup for the token data
    web3 = log["event"].web3
    token_cache: TokenCache = log["context"]
    result = _decode_base(log)

    # Any indexed Solidity event parameter will be in topics data.
    # The first topics (0) is always the event signature.
    event_signature, token0, token1, fee = log["topics"]
    token0_address = convert_uint256_string_to_address(token0)
    token1_address = convert_uint256_string_to_address(token1)

    # Now enhanche data with token information
    token0 = token_cache.get_token_info(web3, token0_address)
    token1 = token_cache.get_token_info(web3, token1_address)

    # Any non-indexed Solidity event parameter will be in the data section.
    # Chop data blob to byte32 entries
    tick_spacing, pool_contract_address = decode_data(log["data"])

    result.update(
        {
            "factory_contract_address": log["address"],
            "pool_contract_address": convert_uint256_bytes_to_address(pool_contract_address),
            "fee": convert_uint256_string_to_int(fee),
            "token0_symbol": token0.symbol,
            "token0_address": token0_address,
            "token1_symbol": token1.symbol,
            "token1_address": token1_address,
        }
    )
    return result


def decode_swap(log: LogResult) -> dict:
    """Process swap event. The event signature is:

    .. code-block::

        event Swap(
            address indexed sender,
            address indexed recipient,
            int256 amount0,
            int256 amount1,
            uint160 sqrtPriceX96,
            uint128 liquidity,
            int24 tick
        );
    """
    result = _decode_base(log)
    amount0, amount1, sqrt_price_x96, liquidity, tick = decode_data(log["data"])

    result.update(
        {
            "pool_contract_address": log["address"],
            "amount0": convert_uint256_bytes_to_int(amount0),
            "amount1": convert_uint256_bytes_to_int(amount1),
            "sqrt_price_x96": convert_uint256_bytes_to_int(sqrt_price_x96),
            "liquidity": convert_uint256_bytes_to_int(liquidity),
            "tick": convert_uint256_bytes_to_int(tick, signed=True),
        }
    )
    return result


def decode_mint(log: LogResult) -> dict:
    """Process mint event. The event signature is:

    .. code-block::

        event Mint(
            address sender,
            address indexed owner,
            int24 indexed tickLower,
            int24 indexed tickUpper,
            uint128 amount,
            uint256 amount0,
            uint256 amount1
        );
    """
    result = _decode_base(log)

    event_signature, owner, tick_lower, tick_upper = log["topics"]
    sender, amount, amount0, amount1 = decode_data(log["data"])

    result.update(
        {
            "pool_contract_address": log["address"],
            "tick_lower": convert_uint256_string_to_int(tick_lower, signed=True),
            "tick_upper": convert_uint256_string_to_int(tick_upper, signed=True),
            "amount": convert_uint256_bytes_to_int(amount),
            "amount0": convert_uint256_bytes_to_int(amount0),
            "amount1": convert_uint256_bytes_to_int(amount1),
        }
    )
    return result


def decode_burn(log: LogResult) -> dict:
    """Process burn event. The event signature is:

    .. code-block::

        event Burn(
            address indexed owner,
            int24 indexed tickLower,
            int24 indexed tickUpper,
            uint128 amount,
            uint256 amount0,
            uint256 amount1
        );
    """
    result = _decode_base(log)

    event_signature, owner, tick_lower, tick_upper = log["topics"]
    amount, amount0, amount1 = decode_data(log["data"])

    result.update(
        {
            "pool_contract_address": log["address"],
            "tick_lower": convert_uint256_string_to_int(tick_lower, signed=True),
            "tick_upper": convert_uint256_string_to_int(tick_upper, signed=True),
            "amount": convert_uint256_bytes_to_int(amount),
            "amount0": convert_uint256_bytes_to_int(amount0),
            "amount1": convert_uint256_bytes_to_int(amount1),
        }
    )
    return result


def get_event_mapping(web3: Web3) -> dict:
    """Returns tracked event types and mapping.

    Currently we are tracking these events:
        - PoolCreated
        - Swap
        - Mint
        - Burn
    """
    Factory = get_contract(web3, "uniswap_v3/UniswapV3Factory.json")
    Pool = get_contract(web3, "uniswap_v3/UniswapV3Pool.json")

    return {
        "PoolCreated": {
            "contract_event": Factory.events.PoolCreated,
            "field_names": [
                "block_number",
                "timestamp",
                "tx_hash",
                "log_index",
                "factory_contract_address",
                "pool_contract_address",
                "fee",
                "token0_address",
                "token0_symbol",
                "token1_address",
                "token1_symbol",
            ],
            "decode_function": decode_pool_created,
        },
        "Swap": {
            "contract_event": Pool.events.Swap,
            "field_names": [
                "block_number",
                "timestamp",
                "tx_hash",
                "log_index",
                "pool_contract_address",
                "amount0",
                "amount1",
                "sqrt_price_x96",
                "liquidity",
                "tick",
            ],
            "decode_function": decode_swap,
        },
        "Mint": {
            "contract_event": Pool.events.Mint,
            "field_names": [
                "block_number",
                "timestamp",
                "tx_hash",
                "log_index",
                "pool_contract_address",
                "tick_lower",
                "tick_upper",
                "amount",
                "amount0",
                "amount1",
            ],
            "decode_function": decode_mint,
        },
        "Burn": {
            "contract_event": Pool.events.Burn,
            "field_names": [
                "block_number",
                "timestamp",
                "tx_hash",
                "log_index",
                "pool_contract_address",
                "tick_lower",
                "tick_upper",
                "amount",
                "amount0",
                "amount1",
            ],
            "decode_function": decode_burn,
        },
    }


def fetch_events_to_csv(
    json_rpc_url: str,
    start_block: int = UNISWAP_V3_FACTORY_CREATED_AT_BLOCK,
    end_block: int = UNISWAP_V3_FACTORY_CREATED_AT_BLOCK + 1000,
    output_folder: str = "/tmp",
):
    """Fetch all tracked Uniswap v3 events to CSV files

    :param json_rpc_url: JSON-RPC URL
    :param start_block: First block to process (inclusive), default is block 12369621 (when Uniswap v3 factory was created on mainnet)
    :param end_block: Last block to process (inclusive), default is block 12370621 (1000 block after default start block)
    :param output_folder: Folder to contain output CSV files, default is /tmp folder
    """
    token_cache = TokenCache()
    web3_factory = TunedWeb3Factory(json_rpc_url)
    web3 = web3_factory(token_cache)
    executor = create_thread_pool_executor(web3_factory, token_cache, max_workers=12)
    event_mapping = get_event_mapping(web3)
    contract_events = [event_data["contract_event"] for event_data in event_mapping.values()]

    # prepare local buffers and files
    buffers = {}
    for event_name, mapping in event_mapping.items():
        file_path = f"{output_folder}/uniswapv3-{event_name}.csv"
        is_file_exists = Path(file_path).exists()
        file_handler = open(file_path, "a")
        csv_writer = csv.DictWriter(file_handler, fieldnames=mapping["field_names"])
        if not is_file_exists:
            csv_writer.writeheader()

        buffers[event_name] = {
            "buffer": [],
            "total": 0,
            "file_handler": file_handler,
            "csv_writer": csv_writer,
        }

    print(f"Starting to read block range {start_block:,} - {end_block:,}")
    with tqdm(total=end_block - start_block) as progress_bar:
        #  1. update the progress bar
        #  2. save any events in the buffer in to a file in one go
        def update_progress(
            current_block,
            start_block,
            end_block,
            chunk_size: int,
            total_events: int,
            last_timestamp: int,
            context: TokenCache,
        ):
            nonlocal buffers

            if last_timestamp:
                # Display progress with the date information
                d = datetime.datetime.utcfromtimestamp(last_timestamp)
                formatted_time = d.strftime("%d-%m-%Y")
                progress_bar.set_description(f"Block: {current_block:,}, events: {total_events:}, time:{formatted_time}")
            else:
                progress_bar.set_description(f"Block: {current_block:,}, events: {total_events:,}")

            progress_bar.update(chunk_size)

            for buffer_data in buffers.values():
                buffer = buffer_data["buffer"]

                # write events to csv
                for entry in buffer:
                    buffer_data["csv_writer"].writerow(entry)
                    buffer_data["total"] += 1

                # then reset buffer
                buffer = []

        # Read specified events in block range
        for log_result in read_events_concurrent(
            executor,
            start_block,
            end_block,
            events=contract_events,
            notify=update_progress,
            chunk_size=100,
            context=token_cache,
        ):
            try:
                # write to correct buffer
                event_name = log_result["event"].event_name
                buffer = buffers[event_name]["buffer"]
                decode_function = event_mapping[event_name]["decode_function"]

                buffer.append(decode_function(log_result))
            except Exception as e:
                raise RuntimeError(f"Could not decode {log_result}") from e

    # close files and print stats
    for event_name, buffer in buffers.items():
        buffer["file_handler"].close()
        print(f"Wrote {buffer['total']} {event_name} events")