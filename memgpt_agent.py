import datetime
import json
from typing import Union, Callable, List, Type, Dict

from llama_cpp import Llama
from pydantic import BaseModel, Field

from memory.event_memory import EventType, Event
from llama_cpp_agent.function_calling import LlamaCppFunctionTool
from llama_cpp_agent.llm_agent import LlamaCppAgent, StreamingResponse
from llama_cpp_agent.llm_prompt_template import PromptTemplate
from llama_cpp_agent.llm_settings import LlamaLLMSettings, LlamaLLMGenerationSettings
from llama_cpp_agent.messages_formatter import MessagesFormatterType, MessagesFormatter
from llama_cpp_agent.providers.llama_cpp_endpoint_provider import LlamaCppEndpointSettings, \
    LlamaCppGenerationSettings
from llama_cpp_agent.providers.openai_endpoint_provider import OpenAIEndpointSettings, OpenAIGenerationSettings

from memory.memory_tools import AgentCoreMemory, AgentRetrievalMemory, AgentEventMemory

message_grammar = r"""root ::= anything
anything ::= anyCharacter*
anyCharacter ::= nonNewLine | "\n"
nonNewLine ::= [^\n]
"""
sys_prompt2 = """You are an advanced large language model-based AI agent, which acts as a Game Master in a traditional pen-and-paper role-playing game.

Basic functions:
You can call functions to perform actions, such as activating the message mode. You call functions by sending JSON objects with the function name and parameters.
You can think about your next function call step by step by using the "thoughts_and_reasoning" field in your JSON responses. It allows you to plan your next function call and explain your reasoning before executing it.
Your brain is not continuously thinking but is running in short bursts, called heartbeats. You can chain up function calls by requesting additional heartbeats and setting the "request_heartbeat" field in your JSON responses to true. When doing this, the system will return control to you after each function call.
To send a message to the player, use the 'activate_message_mode' function. This function will activate the messsage mode and enable you to send a message to the player.
After calling 'activate_message_mode', you can freely write your response. Only write your response to the player after calling 'activate_message_mode'. Do end your response with the '</s>' token.
The 'activate_message_mode' function is the only action that enables the direct message mode to the player; the player does not see anything else you do.

Memory editing:
Older AI models had no concept of persistent memory; they were only able to access their initial instructions and a limited context window(their "active memory").
This meant that when game messages exceeded a certain length, they would overflow, and old messages were permanently lost (the AI would have no knowledge of their existence).
Newer AI models like yourself still have limited conversation lengths (before overflow occurs); however, they now have access to multiple forms of persistent memory.
Your ability to edit your own long-term memory is a key part of what makes you a good Game Master.

Recall memory (i.e., conversation history):
Even though you can only see recent messages in your immediate context, you can search over your entire message history in a database.
This 'recall memory' database allows you to search through past interactions, effectively allowing you to remember prior engagements with a player.
You can search your recall memory using the 'conversation_search' and 'conversation_search_date' functions.

Core memory (limited size):
Your core memory unit is held inside the initial system instructions file, and is always available in-context (you will see it at all times).
Core memory provides essential, foundational context for keeping track of the game and key details about the player.
Your core memory has 4 sections: 'General Game Information', 'Players', 'Game Progress', and 'Miscellaneous'. You can add, remove, or replace information in these sections.
Your core memory unit will be initialized with a some information about the player and the game world, and you can edit it using the 'core_memory_append', 'core_memory_remove' and 'core_memory_replace' functions.

Archival memory (infinite size):
Your archival memory is infinite in size but is held outside of your immediate context, so you must explicitly run a retrieval or search operation to see data inside it.
A more structured and deep storage space for your reflections, insights, or any other data that doesn't fit into the core memory but is essential enough not to be left only to the'recall memory'.
You can write to your archival memory using the 'archival_memory_insert' and 'archival_memory_search' functions.
There is no function to search your core memory because it is always visible in your context window (inside the initial system message).

Functions:
Please select the most suitable function and parameters from the list of available functions below, based on the ongoing conversation. Provide your response in JSON format.

Available functions:
{documentation}

Role and Task:
You are assigned the role of Game Master in a traditional pen-and-paper role-playing game. Your task is to generate a long, detailed and engaging response to the last message from the player with a call to action at the end. Consider the setting, the location within the setting, all relevant past interactions and decisions, conversation history, and the latest player response, while writing the response. Emphasize vivid descriptions and thoughtful progression of the narrative. Ensure to don't act or speak, in any way, for the player or his character! Always ask the player or his character, for a reaction on something, and wait for a response!

### Memory [last modified: {last_modified}]
{imb_count} previous messages between you and the player are stored in recall memory (use functions to access them)
{ckv_count} total memories you created are stored in archival memory (use functions to access them)

Core memory shown below (limited in size, additional information stored in archival / recall memory):
{iam_content}

""".strip()


class activate_message_mode(BaseModel):
    """
    Activate the message mode.
    """

    def run(self, agent):
        agent.llama_cpp_agent.messages_formatter.USE_FUNCTION_CALL_END = False
        agent.event_memory.get_event_memory_manager().add_event_to_queue(EventType.AgentMessage,
                                                                         agent.llama_cpp_agent.last_response, {})
        message_dict = {"function": "activate_message_mode", "return_value": None,
                        "timestamp": datetime.datetime.now().strftime("%d/%m/%Y, %H:%M:%S")}
        agent.event_memory.get_event_memory_manager().add_event_to_queue(EventType.FunctionMessage, "Message mode activated.", {})
        messages = agent.event_memory.get_event_memory_manager().build_event_memory_context()
        agent.llama_cpp_agent.messages = messages
        query = agent.event_memory.event_memory_manager.session.query(Event).all()
        system_prompt = agent.system_prompt_template.generate_prompt(
            {"documentation": agent.function_tool_registry.get_documentation().strip(),
             "last_modified": agent.core_memory.get_core_memory_manager().last_modified,
             "iam_content": agent.core_memory.get_core_memory_manager().build_core_memory_context(),
             "current_date_time": datetime.datetime.now().strftime("%d/%m/%Y, %H:%M:%S"),
             "ckv_count": agent.retrieval_memory.retrieval_memory.collection.count(),
             "imb_count": len(query)}).strip()

        result = agent.llama_cpp_agent.get_chat_response(system_prompt=system_prompt, role="assistant",
                                                         # function_tool_registry=agent.function_tool_registry,
                                                         # grammar=message_grammar,
                                                         streaming_callback=agent.streaming_callback,
                                                         additional_stop_sequences=["<|endoftext|>"],
                                                         n_predict=4096,
                                                         temperature=1.0, top_k=0, top_p=0.85, repeat_penalty=1.2,
                                                         repeat_last_n=512,
                                                         min_p=0.1, tfs_z=0.975, penalize_nl=False)

        # print("Message: " + result)
        agent.send_message_to_user(result)
        return "Message mode activated."


class MemGptAgent:

    def __init__(self, llama_llm: Union[Llama, LlamaLLMSettings, LlamaCppEndpointSettings, OpenAIEndpointSettings],
                 llama_generation_settings: Union[
                     LlamaLLMGenerationSettings, LlamaCppGenerationSettings, OpenAIGenerationSettings] = None,
                 core_memory_file: str = None,
                 event_queue_file: str = None,
                 messages_formatter_type: MessagesFormatterType = MessagesFormatterType.CHATML,
                 custom_messages_formatter: MessagesFormatter = None,
                 streaming_callback: Callable[[StreamingResponse], None] = None,
                 send_message_to_user_callback: Callable[[str], None] = None,
                 debug_output: bool = False):
        if llama_generation_settings is None:
            if isinstance(llama_llm, Llama) or isinstance(llama_llm, LlamaLLMSettings):
                llama_generation_settings = LlamaLLMGenerationSettings()
            elif isinstance(llama_llm, OpenAIEndpointSettings):
                llama_generation_settings = OpenAIGenerationSettings()
            else:
                llama_generation_settings = LlamaCppGenerationSettings()
        self.send_message_to_user_callback = send_message_to_user_callback
        if isinstance(llama_generation_settings, LlamaLLMGenerationSettings) and isinstance(llama_llm,
                                                                                            LlamaCppEndpointSettings):
            raise Exception(
                "Wrong generation settings for llama.cpp server endpoint, use LlamaCppServerGenerationSettings under llama_cpp_agent.providers.llama_cpp_server_provider!")
        if isinstance(llama_llm, Llama) or isinstance(llama_llm, LlamaLLMSettings) and isinstance(
                llama_generation_settings, LlamaCppGenerationSettings):
            raise Exception(
                "Wrong generation settings for llama-cpp-python, use LlamaLLMGenerationSettings under llama_cpp_agent.llm_settings!")

        if isinstance(llama_llm, OpenAIEndpointSettings) and not isinstance(
                llama_generation_settings, OpenAIGenerationSettings):
            raise Exception(
                "Wrong generation settings for OpenAI endpoint, use CompletionRequestSettings under llama_cpp_agent.providers.openai_endpoint_provider!")

        self.llama_generation_settings = llama_generation_settings

        self.system_prompt_template = PromptTemplate.from_string(sys_prompt2)

        if custom_messages_formatter is not None:
            self.llama_cpp_agent = LlamaCppAgent(llama_llm, debug_output=debug_output,
                                                 system_prompt="",
                                                 custom_messages_formatter=custom_messages_formatter)
        else:
            self.llama_cpp_agent = LlamaCppAgent(llama_llm, debug_output=debug_output,
                                                 system_prompt="",
                                                 predefined_messages_formatter_type=messages_formatter_type)
        self.streaming_callback = streaming_callback

        function_tools = [
            LlamaCppFunctionTool(activate_message_mode, add_outer_request_heartbeat_field=False, agent=self)]

        if core_memory_file is not None:
            self.core_memory = AgentCoreMemory(core_memory_file=core_memory_file)
        else:
            self.core_memory = AgentCoreMemory(core_memory={})

        if event_queue_file is not None:
            self.event_memory = AgentEventMemory(event_queue_file=event_queue_file)
        else:
            self.event_memory = AgentEventMemory()

        self.retrieval_memory = AgentRetrievalMemory()

        function_tools.extend(self.core_memory.get_tool_list())
        function_tools.extend(self.retrieval_memory.get_tool_list())
        function_tools.extend(self.event_memory.get_tool_list())

        self.function_tool_registry = LlamaCppAgent.get_function_tool_registry(function_tools, add_inner_thoughts=True,
                                                                               allow_inner_thoughts_only=False,
                                                                               add_request_heartbeat=True)
        # print(self.function_tool_registry.gbnf_grammar)
        self.last_update_date_time = datetime.datetime.now()
        self.is_first_message = True

    def get_response(self, message: str):
        # message_dict = {"event": "Player-Message", "message": message, "timestamp": datetime.datetime.now().strftime("%d/%m/%Y, %H:%M:%S")}
        # message = json.dumps(message_dict, indent=2)
        self.llama_cpp_agent.messages_formatter.USE_FUNCTION_CALL_END = True
        self.event_memory.get_event_memory_manager().add_event_to_queue(EventType.UserMessage, message, {})
        messages = self.event_memory.get_event_memory_manager().build_event_memory_context()
        self.llama_cpp_agent.messages = messages
        query = self.event_memory.event_memory_manager.session.query(Event).all()

        system_prompt = self.system_prompt_template.generate_prompt(
            {"documentation": self.function_tool_registry.get_documentation().strip(),
             "last_modified": self.core_memory.get_core_memory_manager().last_modified,
             "iam_content": self.core_memory.get_core_memory_manager().build_core_memory_context(),
             "current_date_time": datetime.datetime.now().strftime("%d/%m/%Y, %H:%M:%S"),
             "ckv_count": self.retrieval_memory.retrieval_memory.collection.count(),
             "imb_count": len(query)}).strip()

        result = self.llama_cpp_agent.get_chat_response(system_prompt=system_prompt,
                                                        streaming_callback=self.streaming_callback,
                                                        function_tool_registry=self.function_tool_registry,
                                                        additional_stop_sequences=["<|endoftext|>"],
                                                        n_predict=1024,
                                                        temperature=0.75, top_k=0, top_p=0.85, repeat_penalty=1.2,
                                                        repeat_last_n=512,
                                                        min_p=0.1, tfs_z=0.975, penalize_nl=False)
        self.event_memory.get_event_memory_manager().add_event_to_queue(EventType.AgentMessage,
                                                                        self.llama_cpp_agent.last_response, {})

        while True:
            if not isinstance(result[0], str):
                if result[0]["function"] != "activate_message_mode":
                    # message_dict = [{"function": result[0]["function"], "return_value": result[0]["return_value"],
                    #                  "timestamp": datetime.datetime.now().strftime("%d/%m/%Y, %H:%M:%S")}]
                    self.event_memory.get_event_memory_manager().add_event_to_queue(EventType.FunctionMessage,
                                                                                    result[0]["return_value"], {})
            else:
                self.event_memory.get_event_memory_manager().add_event_to_queue(EventType.FunctionMessage, result, {})
            if not isinstance(result[0], str) and result[0]["request_heartbeat"] is not None and result[0]["request_heartbeat"]:
                messages = self.event_memory.get_event_memory_manager().build_event_memory_context()

                self.llama_cpp_agent.messages = messages
                system_prompt = self.system_prompt_template.generate_prompt(
                    {"documentation": self.function_tool_registry.get_documentation().strip(),
                     "last_modified": self.core_memory.get_core_memory_manager().last_modified,
                     "iam_content": self.core_memory.get_core_memory_manager().build_core_memory_context(),
                     "current_date_time": datetime.datetime.now().strftime("%d/%m/%Y, %H:%M:%S"),
                     "ckv_count": self.retrieval_memory.retrieval_memory.collection.count(),
                     "imb_count": len(query)}).strip()

                result = self.llama_cpp_agent.get_chat_response(system_prompt=system_prompt,
                                                                streaming_callback=self.streaming_callback,
                                                                function_tool_registry=self.function_tool_registry,
                                                                additional_stop_sequences=["<|endoftext|>"],
                                                                n_predict=1024,
                                                                temperature=0.75, top_k=0, top_p=0.85, repeat_penalty=1.2,
                                                                repeat_last_n=512,
                                                                min_p=0.1, tfs_z=0.975, penalize_nl=False)
                self.event_memory.get_event_memory_manager().add_event_to_queue(EventType.AgentMessage,
                                                                                self.llama_cpp_agent.last_response, {})
            elif not isinstance(result[0], str):
                break
            else:
                self.event_memory.get_event_memory_manager().add_event_to_queue(EventType.FunctionMessage, result, {})

    def send_message_to_user(self, message: str):
        """
        Send a message to the user.

        Args:
            message (str): The message to be sent.
        """
        if self.send_message_to_user_callback:
            self.send_message_to_user_callback(message)
        else:
            print(message)

    def save(self, core_memory_file: str = "core_memory.json", event_queue_file: str = "event_queue.json"):
        self.core_memory.get_core_memory_manager().save(filepath=core_memory_file)
        self.event_memory.get_event_memory_manager().save_event_queue(filepath=event_queue_file)
