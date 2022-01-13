#!/usr/bin/env python

"""
run_block: Convert an encoded FullBlock from the Chia blockchain into a list of transactions

As input, takes a file containing a [FullBlock](../chia/types/full_block.py) in json format

```
curl --insecure --cert $config_root/config/ssl/full_node/private_full_node.crt \
     --key $config_root/config/ssl/full_node/private_full_node.key \
     -d '{ "header_hash": "'$hash'" }' -H "Content-Type: application/json" \
     -X POST https://localhost:$port/get_block

$ca_root is the directory containing your current Chia config files
$hash is the header_hash of the [BlockRecord](../chia/consensus/block_record.py)
$port is the Full Node RPC API port
```

The `transactions_generator` and `transactions_generator_ref_list` fields of a `FullBlock`
contain the information necessary to produce transaction record details.

`transactions_generator` is CLVM bytecode
`transactions_generator_ref_list` is a list of block heights as `uint32`

When this CLVM code is run in the correct environment, it produces information that can
then be verified by the consensus rules, or used to view some aspects of transaction history.

The information for each spend is an "NPC" (Name, Puzzle, Condition):
        "coin_name": a unique 32 byte identifier
        "conditions": a list of condition expressions, as in [condition_opcodes.py](../chia/types/condition_opcodes.py)
        "puzzle_hash": the sha256 of the CLVM bytecode that controls spending this coin

Condition Opcodes, such as AGG_SIG_ME, or CREATE_COIN are created by running the "puzzle", i.e. the CLVM bytecode
associated with the coin being spent. Condition Opcodes are verified by every client on the network for every spend,
and in this way they control whether a spend is valid or not.

"""
import json
from dataclasses import dataclass
from typing import List, TextIO, Tuple

import click
from chia.consensus.constants import ConsensusConstants
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.full_node.mempool_check_conditions import get_name_puzzle_conditions, get_puzzle_and_solution_for_coin
from chia.types.blockchain_format.program import SerializedProgram
from chia.types.condition_opcodes import ConditionOpcode
from chia.types.condition_with_args import ConditionWithArgs
from chia.types.full_block import FullBlock
from chia.types.generator_types import BlockGenerator, GeneratorArg
from chia.types.name_puzzle_condition import NPC
from chia.util.config import load_config
from chia.util.default_root import DEFAULT_ROOT_PATH
from chia.util.errors import ConsensusError, Err
from chia.util.ints import uint32
from chia.wallet.cat_wallet.cat_utils import match_cat_puzzle


@dataclass
class CAT:
    tail_hash: str
    memo: str
    npc: NPC

    def cat_to_dict(self):
        return {"tail_hash": self.tail_hash, "memo": self.memo, "npc": npc_to_dict(self.npc)}


def condition_with_args_to_dict(condition_with_args: ConditionWithArgs):
    return {
        "condition_opcode": condition_with_args.opcode.name,
        "arguments": [arg.hex() for arg in condition_with_args.vars],
    }


def condition_list_to_dict(condition_list: Tuple[ConditionOpcode, List[ConditionWithArgs]]):
    assert all([condition_list[0] == cwa.opcode for cwa in condition_list[1]])
    return [condition_with_args_to_dict(cwa) for cwa in condition_list[1]]


def npc_to_dict(npc: NPC):
    return {
        "coin_name": npc.coin_name.hex(),
        "conditions": [{"condition_type": c[0].name, "conditions": condition_list_to_dict(c)} for c in npc.conditions],
        "puzzle_hash": npc.puzzle_hash.hex(),
    }


def run_generator(block_generator: BlockGenerator, constants: ConsensusConstants) -> List[CAT]:
    npc_result = get_name_puzzle_conditions(
        block_generator,
        constants.MAX_BLOCK_COST_CLVM,  # min(self.constants.MAX_BLOCK_COST_CLVM, block.transactions_info.cost),
        cost_per_byte=constants.COST_PER_BYTE,
        mempool_mode=False,
    )
    if npc_result.error is not None:
        raise ConsensusError(Err(npc_result.error))

    cat_list: List[CAT] = []
    for npc in npc_result.npc_list:
        _, puzzle, solution = get_puzzle_and_solution_for_coin(
            block_generator, coin_name=npc.coin_name, max_cost=constants.MAX_BLOCK_COST_CLVM
        )
        matched, curried_args = match_cat_puzzle(puzzle)

        if matched:
            _, tail_hash, _ = curried_args
            memo = ""

            # do somethign like this to get the memo out
            result = puzzle.run(solution)
            for condition in result.as_python():
                if condition[0] == ConditionOpcode.CREATE_COIN and len(condition) >= 4:
                    # If only 3 elements (opcode + 2 args), there is no memo, this is ph, amount
                    if type(condition[3]) != list:
                        # If it's not a list, it's not the correct format
                        continue

                    # special retirement address
                    if condition[3][0].hex() == "0000000000000000000000000000000000000000000000000000000000000000":
                        if len(condition[3]) >= 2:
                            memo = condition[3][1].decode("utf-8")

                        # technically there could be more such create_coin ops in the list but our wallet does not
                        # so leaving it for the future
                        break

            cat_list.append(CAT(tail_hash=bytes(tail_hash).hex()[2:], memo=memo, npc=npc))

    return cat_list


def ref_list_to_args(ref_list: List[uint32]):
    args = []
    for height in ref_list:
        with open(f"{height}.json", "r") as f:
            program_str = json.load(f)["block"]["transactions_generator"]
            arg = GeneratorArg(height, SerializedProgram.fromhex(program_str))
            args.append(arg)
    return args


def run_full_block(block: FullBlock, constants: ConsensusConstants) -> List[CAT]:
    generator_args = ref_list_to_args(block.transactions_generator_ref_list)
    if block.transactions_generator is None:
        raise RuntimeError("transactions_generator of FullBlock is null")
    block_generator = BlockGenerator(block.transactions_generator, generator_args)
    return run_generator(block_generator, constants)


def run_generator_with_args(
    generator_program_hex: str, generator_args: List[GeneratorArg], constants: ConsensusConstants
) -> List[CAT]:
    if not generator_program_hex:
        return []
    generator_program = SerializedProgram.fromhex(generator_program_hex)
    block_generator = BlockGenerator(generator_program, generator_args)
    return run_generator(block_generator, constants)


@click.command()
@click.argument("file", type=click.File("rb"))
def cmd_run_json_block_file(file):
    """`file` is a file containing a FullBlock in JSON format"""
    return run_json_block_file(file)


def run_json_block_file(file: TextIO):
    _, constants = get_config_and_constants()
    full_block = json.load(file)
    ref_list = full_block["block"]["transactions_generator_ref_list"]
    args = ref_list_to_args(ref_list)
    cat_list: List[CAT] = run_generator_with_args(full_block["block"]["transactions_generator"], args, constants)
    cat_list_json = json.dumps([cat.cat_to_dict() for cat in cat_list])
    print(cat_list_json)


def get_config_and_constants():
    config = load_config(DEFAULT_ROOT_PATH, "config.yaml")
    network = config["selected_network"]
    overrides = config["network_overrides"]["constants"][network]
    updated_constants = DEFAULT_CONSTANTS.replace_str_to_bytes(**overrides)
    return config, updated_constants


if __name__ == "__main__":
    cmd_run_json_block_file()  # pylint: disable=no-value-for-parameter
