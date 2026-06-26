"""Lansing firmware error catalog."""

from __future__ import annotations

from ..errors import ErrorInfo


LANSING_ERROR_INFO: dict[str, ErrorInfo] = {
    "BAD_COMMAND": ErrorInfo(
        "A received text line could not be parsed as a valid command.",
        "Command name was shorter than 3 characters, longer than 3 characters, malformed, or otherwise failed parser validation.",
        "Send a newline-terminated 3-letter text command. Check that fields are separated by spaces, tabs, or commas.",
    ),
    "LINE_TOO_LONG": ErrorInfo(
        "The incoming text line exceeded the serial line buffer.",
        "The command line was longer than the firmware serial buffer.",
        "Shorten the command. For high-speed actuator updates, use binary stream mode.",
    ),
    "UNKNOWN_COMMAND": ErrorInfo(
        "The command parsed correctly, but no handler exists for that command.",
        "Typo, unsupported command, or old host software using a removed command name.",
        "Compare the command against the Lansing command list. All command names are exactly 3 characters.",
    ),
    "VER_PARAM_COUNT": ErrorInfo(
        "Version query received parameters, but it expects none.",
        "Host sent extra fields after the version command.",
        "Send the version command by itself.",
    ),
    "STR_PARAM_COUNT": ErrorInfo(
        "Binary stream mode request received parameters, but it expects none.",
        "Host sent extra fields after the stream command.",
        "Send the stream command by itself, then switch the host to binary packet writes after OK:STR.",
    ),
    "RBT_PARAM_COUNT": ErrorInfo(
        "Reboot command received parameters, but it expects none.",
        "Host sent extra fields after the reboot command.",
        "Send the reboot command by itself.",
    ),
    "PSU_PARAM_COUNT": ErrorInfo(
        "PSU on/off command received the wrong number of parameters.",
        "More than one parameter was sent.",
        "Send no parameter to read state, or one parameter: ON, OFF, 1, or 0.",
    ),
    "PSU_PARAM_VALUE": ErrorInfo(
        "PSU command parameter was not recognized.",
        "Parameter was not ON, OFF, 1, or 0.",
        "Use one of the accepted values.",
    ),
    "PSC_PARAM_COUNT": ErrorInfo(
        "PSU connection command received the wrong number of parameters.",
        "More than one parameter was sent.",
        "Send no parameter to read state, or one parameter: ON, OFF, 1, or 0.",
    ),
    "PSC_PSU_OFF": ErrorInfo(
        "The host tried to connect PSU output while the PSU was off.",
        "PSU output cannot be connected unless the PSU is already on.",
        "Turn the PSU on first, then connect the PSU output.",
    ),
    "PSC_PARAM_VALUE": ErrorInfo(
        "PSU connection parameter was not recognized.",
        "Parameter was not ON, OFF, 1, or 0.",
        "Use one of the accepted values.",
    ),
    "CUR_PARAM_COUNT": ErrorInfo(
        "Current read command received parameters, but it expects none.",
        "Host may be using an older protocol where current diagnosis was part of current read.",
        "Send the current-read command by itself. Use actuator diagnosis for actuator-specific current testing.",
    ),
    "VLT_PARAM_COUNT": ErrorInfo(
        "Voltage read command received too many parameters.",
        "More than one measurement-time parameter was sent.",
        "Send no parameter for a quick read, or one numeric measurement time in milliseconds.",
    ),
    "VLT_PARAM_VALUE": ErrorInfo(
        "Voltage measurement-time parameter was invalid.",
        "Parameter was non-numeric or less than 1.",
        "Send a positive integer measurement time in milliseconds.",
    ),
    "ACT_PARAM_COUNT": ErrorInfo(
        "Normal actuator command received too many parameters.",
        "More than two parameters were sent.",
        "Send no parameters to read all values, one actuator number to read one value, or actuator number plus output value to set.",
    ),
    "ACT_ACTUATOR": ErrorInfo(
        "Actuator parameter was invalid.",
        "Actuator was non-numeric, negative, or outside 0..23.",
        "Use actuator numbers 0 through 23.",
    ),
    "ACT_VALUE": ErrorInfo(
        "Actuator output value was invalid.",
        "Value was non-numeric, negative, or greater than 255.",
        "Use values 0 through 255.",
    ),
    "ACT_PSU_OFF": ErrorInfo(
        "Host tried to set an actuator while the PSU was off.",
        "Normal actuator writes require the PSU to be on.",
        "Turn the PSU on first.",
    ),
    "ACT_PSU_DISCONNECTED": ErrorInfo(
        "Host tried to set an actuator while PSU output was disconnected.",
        "Normal actuator writes require PSU output to be connected.",
        "Connect PSU output first.",
    ),
    "ACT_FAILED": ErrorInfo(
        "The actuator set request was valid, but firmware refused to apply it.",
        "Most commonly, the actuator is currently discharging and cannot be re-enabled yet.",
        "Wait for discharge to finish, or check status for discharge time remaining.",
    ),
    "OUT_PARAM_COUNT": ErrorInfo(
        "Manual output command received an unsupported number of parameters.",
        "Two parameters or more than three parameters were sent.",
        "Send no parameters to read all electrode pairs, one actuator number to read one pair, or actuator number plus positive and negative values to write.",
    ),
    "OUT_ACTUATOR": ErrorInfo(
        "Manual output actuator parameter was invalid.",
        "Actuator was non-numeric, negative, or outside 0..23.",
        "Use actuator numbers 0 through 23.",
    ),
    "OUT_POS_VALUE": ErrorInfo(
        "Manual positive electrode value was invalid.",
        "Positive value was non-numeric, negative, or greater than 255.",
        "Use values 0 through 255.",
    ),
    "OUT_NEG_VALUE": ErrorInfo(
        "Manual negative electrode value was invalid.",
        "Negative value was non-numeric, negative, or greater than 255.",
        "Use values 0 through 255.",
    ),
    "OUT_SAFETY_ON": ErrorInfo(
        "Manual output write was blocked by the safety flag.",
        "Safety defaults on at boot and blocks direct electrode writes.",
        "Disable safety through configuration only during controlled bench testing.",
    ),
    "OUT_FAILED": ErrorInfo(
        "Manual output write was valid but the raw electrode write failed.",
        "Invalid internal state or failed lower-level validation.",
        "Check actuator number and firmware status. Reboot if state appears inconsistent.",
    ),
    "INI_PARAM_COUNT": ErrorInfo(
        "Initialization command received the wrong number of parameters.",
        "No actuator number or more than one parameter was sent.",
        "Send exactly one actuator number.",
    ),
    "INI_ACTUATOR": ErrorInfo(
        "Initialization actuator parameter was invalid.",
        "Actuator was non-numeric, negative, or outside 0..23.",
        "Use actuator numbers 0 through 23.",
    ),
    "INI_PSU_OFF": ErrorInfo(
        "Host tried to initialize an actuator while PSU was off.",
        "Initialization drives the actuator and requires power.",
        "Turn the PSU on first.",
    ),
    "INI_PSU_DISCONNECTED": ErrorInfo(
        "Host tried to initialize an actuator while PSU output was disconnected.",
        "Initialization drives the actuator and requires connected output.",
        "Connect PSU output first.",
    ),
    "INI_FAILED": ErrorInfo(
        "Initialization started but failed during one of its drive/off cycles.",
        "A lower-level actuator write failed, or firmware timed out while waiting for discharge between pulses.",
        "Check status and debug output, wait for discharge to finish, then retry.",
    ),
    "DIA_PARAM_COUNT": ErrorInfo(
        "Diagnosis command received the wrong number of parameters.",
        "No actuator number or more than one parameter was sent.",
        "Send exactly one actuator number.",
    ),
    "DIA_ACTUATOR": ErrorInfo(
        "Diagnosis actuator parameter was invalid.",
        "Actuator was non-numeric, negative, or outside 0..23.",
        "Use actuator numbers 0 through 23.",
    ),
    "DIA_PSU_OFF": ErrorInfo(
        "Host tried to diagnose an actuator while PSU was off.",
        "Diagnosis drives the actuator and requires power.",
        "Turn the PSU on first.",
    ),
    "DIA_PSU_DISCONNECTED": ErrorInfo(
        "Host tried to diagnose an actuator while PSU output was disconnected.",
        "Diagnosis drives the actuator and requires connected output.",
        "Connect PSU output first.",
    ),
    "DIA_FAILED": ErrorInfo(
        "Diagnosis could not complete.",
        "Firmware could not bring actuators to idle, activate the actuator, or start discharge measurement.",
        "Check status for active/discharging actuators, wait for discharge completion, then retry.",
    ),
    "TIM_PARAM_COUNT": ErrorInfo(
        "Runtime query received too many parameters.",
        "More than one parameter was sent.",
        "Send no parameters for all runtimes, or one actuator number for one runtime.",
    ),
    "TIM_ACTUATOR": ErrorInfo(
        "Runtime actuator parameter was invalid.",
        "Actuator was non-numeric, negative, or outside 0..23.",
        "Use actuator numbers 0 through 23.",
    ),
    "RST_PARAM_COUNT": ErrorInfo(
        "Runtime reset command received parameters, but it expects none.",
        "Host attempted a per-actuator reset or sent extra fields.",
        "Send reset with no parameters. Individual runtime reset is done through actuator initialization.",
    ),
    "CFG_PARAM_COUNT": ErrorInfo(
        "Configuration command received the wrong number of parameters.",
        "No key, or more than two fields, were sent.",
        "Send one key to read, or key plus value to write.",
    ),
    "CFG_KEY": ErrorInfo(
        "Configuration key was not recognized.",
        "Key was not one of the supported configuration keys.",
        "Use MAX, DIS, SAFE, or DEBUG.",
    ),
    "CFG_VALUE": ErrorInfo(
        "Configuration value was invalid.",
        "Numeric config received a non-numeric or negative value, or a boolean config received a bad value.",
        "Use a non-negative integer for time values. Use ON, OFF, 1, or 0 for boolean values.",
    ),
    "STS_PARAM_COUNT": ErrorInfo(
        "Status command received parameters, but it expects none.",
        "Host sent extra fields.",
        "Send status with no parameters.",
    ),
}
