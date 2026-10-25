"""Consistent help aliases without rewriting literal workload arguments."""
import argparse


class ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        help_enabled = kwargs.pop("add_help", True)
        super().__init__(*args, add_help=False, **kwargs)
        if help_enabled:
            self.add_argument("-h", "-?", "--help", "--?", action="help",
                              help="show this help message and exit")


def help_aliases(arguments):
    result = list(arguments)
    for index, value in enumerate(result):
        if value == "--":
            break
        if value in {"-?", "--?"}:
            result[index] = "--help"
    return result
