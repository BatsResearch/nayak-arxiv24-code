# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import argparse
import copy
import importlib
import logging
import os
from dataclasses import dataclass, field

# from itertools import chain
from os.path import exists, isdir, join
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

import bitsandbytes as bnb
import datasets
import torch
import transformers
from datasets import DatasetDict, load_dataset
from packaging import version
from peft import LoraConfig, PeftModel, get_peft_model, prepare_model_for_kbit_training
from peft.tuners.lora import LoraLayer
from torch.nn.utils.rnn import pad_sequence
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    LlamaTokenizer,
    Seq2SeqTrainer,
    set_seed,
)

# from transformers.data.data_collator import DataCollator
# from transformers.modeling_utils import PreTrainedModel
from transformers.trainer_callback import TrainerCallback
from transformers.trainer_utils import PREFIX_CHECKPOINT_DIR, EvalPrediction


def is_ipex_available():
    def get_major_and_minor_from_version(full_version):
        return (
            str(version.parse(full_version).major)
            + "."
            + str(version.parse(full_version).minor)
        )

    _torch_version = importlib.metadata.version("torch")
    if importlib.util.find_spec("intel_extension_for_pytorch") is None:
        return False
    _ipex_version = "N/A"
    try:
        _ipex_version = importlib.metadata.version("intel_extension_for_pytorch")
    except importlib.metadata.PackageNotFoundError:
        return False
    torch_major_and_minor = get_major_and_minor_from_version(_torch_version)
    ipex_major_and_minor = get_major_and_minor_from_version(_ipex_version)
    if torch_major_and_minor != ipex_major_and_minor:
        return False
    return True


if torch.cuda.is_available():
    torch.backends.cuda.matmul.allow_tf32 = True

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
    datefmt="%m/%d/%Y %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

IGNORE_INDEX = -100
DEFAULT_PAD_TOKEN = "[PAD]"

# TODO: change these defaults
@dataclass
class ModelArguments:
    model_name_or_path: Optional[str] = field(default="mistralai/Mistral-7B-v0.1")
    trust_remote_code: Optional[bool] = field(
        default=False,
        metadata={
            "help": "Enable unpickling of arbitrary code in AutoModelForCausalLM#from_pretrained."
        },
    )
    use_auth_token: Optional[bool] = field(
        default=False,
        metadata={"help": "Enables using Huggingface auth token from Git Credentials."},
    )


@dataclass
class DataArguments:
    max_train_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of training examples to this "
            "value if set."
        },
    )
    max_eval_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of evaluation examples to this "
            "value if set."
        },
    )
    source_max_len: int = field(
        default=1024,
        metadata={
            "help": "Maximum source sequence length. Sequences will be right padded (and possibly truncated)."
        },
    )
    target_max_len: int = field(
        default=1024,
        metadata={
            "help": "Maximum target sequence length. Sequences will be right padded (and possibly truncated)."
        },
    )
    training_type: str = field(
        default="instruct",
        metadata={"help": "Either instruct or bonito_training"},
    )
    supervision_source: str = field(
        default="bonito",
        metadata={"help": "Which dataset to finetune on. See datamodule for options."},
    )
    dataset: str = field(
        default="pubmed_qa",
        metadata={"help": "Which dataset to finetune on. See datamodule for options."},
    )
    custom_dataset_path: str = field(
        default=None,
        metadata={"help": "Path to a custom dataset to use for training."},
    )

    def __post_init__(self):
        valid_training_types = ["instruct", "bonito_training"]
        valid_supervision_sources = [
            "bonito",
            "dapt",
            "mistral_instruct",
            "zephyr_beta",
            "p3",
        ]

        if self.training_type not in valid_training_types:
            raise ValueError(
                f"training_type must be one of {valid_training_types}, got '{self.training_type}'"
            )

        if self.supervision_source not in valid_supervision_sources:
            raise ValueError(
                f"supervision_source must be one of {valid_supervision_sources}, got '{self.supervision_source}'"
            )


@dataclass
class TrainingArguments(transformers.Seq2SeqTrainingArguments):
    cache_dir: Optional[str] = field(default=None)
    train_on_source: Optional[bool] = field(
        default=False,
        metadata={
            "help": "Whether to train on the input in addition to the target text."
        },
    )

    full_finetune: bool = field(
        default=False, metadata={"help": "Finetune the entire model without adapters."}
    )
    adam8bit: bool = field(default=False, metadata={"help": "Use 8-bit adam."})
    double_quant: bool = field(
        default=True,
        metadata={
            "help": "Compress the quantization statistics through double quantization."
        },
    )
    quant_type: str = field(
        default="nf4",
        metadata={
            "help": "Quantization data type to use. Should be one of `fp4` or `nf4`."
        },
    )
    bits: int = field(default=4, metadata={"help": "How many bits to use."})
    lora_r: int = field(default=64, metadata={"help": "Lora R dimension."})
    lora_alpha: float = field(default=16, metadata={"help": " Lora alpha."})
    lora_dropout: float = field(default=0.0, metadata={"help": "Lora dropout."})
    max_memory_MB: int = field(default=80000, metadata={"help": "Free memory per gpu."})
    report_to: str = field(
        default="none",
        metadata={"help": "To use wandb or something else for reporting."},
    )
    checkpoint_model_id_or_path: str = field(
        default=None, metadata={"help": "the pretrained checkpoint dir to load from"}
    )
    huggingface_checkpoint: str = field(
        default=None, metadata={"help": "the pretrained checkpoint dir to load from"}
    )
    output_dir: str = field(
        default="./output", metadata={"help": "The output dir for logs and checkpoints"}
    )
    optim: str = field(
        default="paged_adamw_32bit", metadata={"help": "The optimizer to be used"}
    )
    per_device_train_batch_size: int = field(
        default=1,
        metadata={
            "help": "The training batch size per GPU. Increase for better speed."
        },
    )
    gradient_accumulation_steps: int = field(
        default=16,
        metadata={
            "help": "How many gradients to accumulate before to perform an optimizer step"
        },
    )
    max_steps: Optional[int] = field(
        default=10000, metadata={"help": "How many optimizer update steps to take"}
    )
    num_train_epochs: Optional[int] = field(
        default=1, metadata={"help": "How many epochs to train for"}
    )
    weight_decay: float = field(
        default=0.0, metadata={"help": "The L2 weight decay rate of AdamW"}
    )  # use lora dropout instead for regularization if needed
    learning_rate: float = field(default=0.0001, metadata={"help": "The learnign rate"})
    remove_unused_columns: bool = field(
        default=False,
        metadata={"help": "Removed unused columns. Needed to make this codebase work."},
    )
    ddp_find_unused_parameter: bool = field(
        default=False, metadata={"help": "Find unused parameters in DDP training."}
    )
    max_grad_norm: float = field(
        default=0.3,
        metadata={
            "help": "Gradient clipping max norm. This is tuned and works well for all models tested."
        },
    )
    gradient_checkpointing: bool = field(
        default=True,
        metadata={"help": "Use gradient checkpointing. You want to use this."},
    )
    do_train: bool = field(
        default=True,
        metadata={"help": "To train or not to train, that is the question?"},
    )
    lr_scheduler_type: str = field(
        default="linear",
        metadata={
            "help": "Learning rate schedule. Constant a bit better than cosine, and has advantage for analysis"
        },
    )
    warmup_ratio: float = field(
        default=0.0, metadata={"help": "Fraction of steps to do a warmup for"}
    )
    logging_steps: int = field(
        default=10,
        metadata={"help": "The frequency of update steps after which to log the loss"},
    )
    group_by_length: bool = field(
        default=True,
        metadata={
            "help": "Group sequences into batches with same length. Saves memory and speeds up training considerably."
        },
    )
    save_strategy: str = field(
        default="steps", metadata={"help": "When to save checkpoints"}
    )
    save_steps: Optional[int] = field(
        default=10000, metadata={"help": "How often to save a model"}
    )
    save_total_limit: int = field(
        default=1,
        metadata={
            "help": "How many checkpoints to save before the oldest is overwritten"
        },
    )


def find_all_linear_names(args, model):
    cls = (
        bnb.nn.Linear4bit
        if args.bits == 4
        else (bnb.nn.Linear8bitLt if args.bits == 8 else torch.nn.Linear)
    )
    lora_module_names = set()
    for name, module in model.named_modules():
        if isinstance(module, cls):
            names = name.split(".")
            lora_module_names.add(names[0] if len(names) == 1 else names[-1])

    if "lm_head" in lora_module_names:  # needed for 16-bit
        lora_module_names.remove("lm_head")
    return list(lora_module_names)


class SavePeftModelCallback(transformers.TrainerCallback):
    def save_model(self, args, state, kwargs):
        print("Saving PEFT checkpoint...")
        if state.best_model_checkpoint is not None:
            checkpoint_folder = os.path.join(
                state.best_model_checkpoint, "adapter_model"
            )
        else:
            # TODO: change this
            checkpoint_folder = os.path.join(
                args.output_dir, f"{PREFIX_CHECKPOINT_DIR}-{state.global_step}"
            )

        peft_model_path = os.path.join(checkpoint_folder, "adapter_model")
        kwargs["model"].save_pretrained(peft_model_path)

        pytorch_model_path = os.path.join(checkpoint_folder, "pytorch_model.bin")
        if os.path.exists(pytorch_model_path):
            os.remove(pytorch_model_path)

    def on_save(self, args, state, control, **kwargs):
        self.save_model(args, state, kwargs)
        return control

    def on_train_end(self, args, state, control, **kwargs):
        def touch(fname, times=None):
            with open(fname, "a"):
                os.utime(fname, times)

        touch(join(args.output_dir, "completed"))
        self.save_model(args, state, kwargs)


class SavePeftModelAtEndCallback(TrainerCallback):
    def on_train_end(self, args, state, control, **kwargs):
        peft_model_path = os.path.join(args.output_dir)
        kwargs["model"].save_pretrained(peft_model_path)
        return control


def smart_tokenizer_and_embedding_resize(
    special_tokens_dict: Dict,
    tokenizer: transformers.PreTrainedTokenizer,
    model: transformers.PreTrainedModel,
):
    """Resize tokenizer and embedding.

    Note: This is the unoptimized version that may make your embedding size not be divisible by 64.
    """
    num_new_tokens = tokenizer.add_special_tokens(special_tokens_dict)
    model.resize_token_embeddings(len(tokenizer))

    if num_new_tokens > 0:
        input_embeddings_data = model.get_input_embeddings().weight.data
        output_embeddings_data = model.get_output_embeddings().weight.data

        input_embeddings_avg = input_embeddings_data[:-num_new_tokens].mean(
            dim=0, keepdim=True
        )
        output_embeddings_avg = output_embeddings_data[:-num_new_tokens].mean(
            dim=0, keepdim=True
        )

        input_embeddings_data[-num_new_tokens:] = input_embeddings_avg
        output_embeddings_data[-num_new_tokens:] = output_embeddings_avg


def get_accelerate_model(args, checkpoint_model_id_or_path, trainable=True):

    if torch.cuda.is_available():
        n_gpus = torch.cuda.device_count()
    if is_ipex_available() and torch.xpu.is_available():
        n_gpus = torch.xpu.device_count()

    max_memory = f"{args.max_memory_MB}MB"
    max_memory = {i: max_memory for i in range(n_gpus)}
    device_map = "auto"

    # if we are in a distributed setting, we need to set the device map and max memory per device
    if os.environ.get("LOCAL_RANK") is not None:
        local_rank = int(os.environ.get("LOCAL_RANK", "0"))
        device_map = {"": local_rank}
        max_memory = {"": max_memory[local_rank]}

    if args.full_finetune:
        assert args.bits in [16, 32]

    print(f"loading base model {args.model_name_or_path}...")
    compute_dtype = (
        torch.float16 if args.fp16 else (torch.bfloat16 if args.bf16 else torch.float32)
    )
    model = AutoModelForCausalLM.from_pretrained(
        args.model_name_or_path,
        cache_dir=args.cache_dir,
        load_in_4bit=args.bits == 4,
        load_in_8bit=args.bits == 8,
        device_map=device_map,
        max_memory=max_memory,
        quantization_config=BitsAndBytesConfig(
            load_in_4bit=args.bits == 4,
            load_in_8bit=args.bits == 8,
            llm_int8_threshold=6.0,
            llm_int8_has_fp16_weight=False,
            bnb_4bit_compute_dtype=compute_dtype,
            bnb_4bit_use_double_quant=args.double_quant,
            bnb_4bit_quant_type=args.quant_type,
        ),
        torch_dtype=(
            torch.float32
            if args.fp16
            else (torch.bfloat16 if args.bf16 else torch.float32)
        ),
        trust_remote_code=args.trust_remote_code,
        use_auth_token=args.use_auth_token,
    )
    if compute_dtype == torch.float16 and args.bits == 4:
        if torch.cuda.is_bf16_supported():
            print("=" * 80)
            print(
                "Your GPU supports bfloat16, you can accelerate training with the argument --bf16"
            )
            print("=" * 80)

    if compute_dtype == torch.float16 and (
        is_ipex_available() and torch.xpu.is_available()
    ):
        compute_dtype = torch.bfloat16
        print("Intel XPU does not support float16 yet, so switching to bfloat16")

    setattr(model, "model_parallel", True)
    setattr(model, "is_parallelizable", True)

    model.config.torch_dtype = (
        torch.float32 if args.fp16 else (torch.bfloat16 if args.bf16 else torch.float32)
    )

    # Tokenizer
    tokenizer = AutoTokenizer.from_pretrained(
        args.model_name_or_path,
        cache_dir=args.cache_dir,
        trust_remote_code=args.trust_remote_code,
        use_auth_token=args.use_auth_token,
    )

    if tokenizer._pad_token is None:
        smart_tokenizer_and_embedding_resize(
            special_tokens_dict=dict(pad_token=DEFAULT_PAD_TOKEN),
            tokenizer=tokenizer,
            model=model,
        )

    if "llama" in args.model_name_or_path or isinstance(tokenizer, LlamaTokenizer):
        # LLaMA tokenizer may not have correct special tokens set.
        # Check and add them if missing to prevent them from being parsed into different tokens.
        # Note that these are present in the vocabulary.
        # Note also that `model.config.pad_token_id` is 0 which corresponds to `<unk>` token.
        print("Adding special tokens.")
        tokenizer.add_special_tokens(
            {
                "eos_token": tokenizer.convert_ids_to_tokens(model.config.eos_token_id),
                "bos_token": tokenizer.convert_ids_to_tokens(model.config.bos_token_id),
                "unk_token": tokenizer.convert_ids_to_tokens(tokenizer.pad_token_id),
            }
        )

    if not args.full_finetune:
        model = prepare_model_for_kbit_training(
            model, use_gradient_checkpointing=args.gradient_checkpointing
        )
        if checkpoint_model_id_or_path is not None:
            print("Loading adapters from checkpoint.")
            # model = PeftModel.from_pretrained(
            #     model, join(checkpoint_dir, "adapter_model"), is_trainable=trainable
            # )
            model = PeftModel.from_pretrained(
                model, checkpoint_model_id_or_path, is_trainable=trainable
            )
        else:
            print(f"adding LoRA modules...")
            modules = find_all_linear_names(args, model)
            config = LoraConfig(
                r=args.lora_r,
                lora_alpha=args.lora_alpha,
                target_modules=modules,
                lora_dropout=args.lora_dropout,
                bias="none",
                task_type="CAUSAL_LM",
            )
            model = get_peft_model(model, config)

    for name, module in model.named_modules():
        if isinstance(module, LoraLayer):
            if args.bf16:
                module = module.to(torch.bfloat16)
        if "norm" in name:
            module = module.to(torch.float32)
        if "lm_head" in name or "embed_tokens" in name:
            if hasattr(module, "weight"):
                if args.bf16 and module.weight.dtype == torch.float32:
                    module = module.to(torch.bfloat16)
    return model, tokenizer


def print_trainable_parameters(args, model):
    """
    Prints the number of trainable parameters in the model.
    """
    trainable_params = 0
    all_param = 0
    for _, param in model.named_parameters():
        all_param += param.numel()
        if param.requires_grad:
            trainable_params += param.numel()
    if args.bits == 4:
        trainable_params /= 2
    print(
        f"trainable params: {trainable_params} || "
        f"all params: {all_param} || "
        f"trainable: {100 * trainable_params / all_param}"
    )


@dataclass
class DataCollatorForCausalLM(object):
    tokenizer: transformers.PreTrainedTokenizer
    source_max_len: int
    target_max_len: int
    train_on_source: bool
    predict_with_generate: bool
    model_name_or_path: str

    def process_instances(self, instances: Sequence[Dict]):
        sources = [
            f"{self.tokenizer.bos_token}{example['input']}" for example in instances
        ]
        targets = [
            f"{example['output']}{self.tokenizer.eos_token}" for example in instances
        ]
        # Tokenize
        tokenized_sources_with_prompt = self.tokenizer(
            sources,
            max_length=self.source_max_len,
            truncation=True,
            add_special_tokens=False,
        )
        tokenized_targets = self.tokenizer(
            targets,
            max_length=self.target_max_len,
            truncation=True,
            add_special_tokens=False,
        )
        # Build the input and labels for causal LM
        input_ids = []
        labels = []
        for tokenized_source, tokenized_target in zip(
            tokenized_sources_with_prompt["input_ids"], tokenized_targets["input_ids"]
        ):
            if not self.predict_with_generate:
                input_ids.append(torch.tensor(tokenized_source + tokenized_target))
                if not self.train_on_source:
                    labels.append(
                        torch.tensor(
                            [IGNORE_INDEX for _ in range(len(tokenized_source))]
                            + copy.deepcopy(tokenized_target)
                        )
                    )
                else:
                    labels.append(
                        torch.tensor(copy.deepcopy(tokenized_source + tokenized_target))
                    )
            else:
                input_ids.append(torch.tensor(tokenized_source))
        # Apply padding
        input_ids = pad_sequence(
            input_ids, batch_first=True, padding_value=self.tokenizer.pad_token_id
        )
        labels = (
            pad_sequence(labels, batch_first=True, padding_value=IGNORE_INDEX)
            if not self.predict_with_generate
            else None
        )
        data_dict = {
            "input_ids": input_ids,
            "attention_mask": input_ids.ne(self.tokenizer.pad_token_id),
        }
        if labels is not None:
            data_dict["labels"] = labels

        return data_dict

    def __call__(self, instances: Sequence[Dict]) -> Dict[str, torch.Tensor]:
        data_dict = self.process_instances(instances)

        return data_dict


def make_data_module(tokenizer: transformers.PreTrainedTokenizer, args) -> Dict:
    def load_data():
        if args.training_type == "instruct":
            if args.custom_dataset_path is not None:
                return datasets.load_from_disk(args.custom_dataset_path)
            else:
                if args.supervision_source == "p3":
                    return load_dataset("BatsResearch/bonito-experiment", "p3_1_6M")
                elif args.supervision_source == "dapt":
                    config_name = f"unannotated_{args.dataset}"
                    return load_dataset("BatsResearch/bonito-experiment", config_name)
                else:
                    config_name = f"{args.supervision_source}_{args.dataset}"
                    return load_dataset("BatsResearch/bonito-experiment", config_name)

        elif args.training_type == "bonito_training":
            return load_dataset("BatsResearch/ctga-v1")

    def format_dataset(dataset, add_prompts=True):
        columns = (
            dataset.column_names["train"]
            if "train" in dataset
            else dataset.column_names
        )

        def preprocess_function(examples):
            bs = len(examples[columns[0]])
            inputs = []
            outputs = []
            for i in range(bs):
                input_text = (
                    "<|input|>\n" + examples["input"][i].strip() + "\n<|output|>\n"
                )
                target_text = examples["output"][i].strip()
                inputs.append(input_text)
                outputs.append(target_text)

            return {
                "input": inputs,
                "output": outputs,
            }

        def preprocess_mistral_instruct_function(examples):
            bs = len(examples[columns[0]])
            inputs = []
            outputs = []
            for i in range(bs):
                input_text = "[INST] " + examples["input"][i].strip() + " [/INST]"
                target_text = examples["output"][i].strip()
                inputs.append(input_text)
                outputs.append(target_text)

            return {
                "input": inputs,
                "output": outputs,
            }

        if add_prompts:
            if args.model_name_or_path == "mistralai/Mistral-7B-Instruct-v0.2":
                print("mistral instruct")
                dataset = dataset.map(
                    preprocess_mistral_instruct_function,
                    batched=True,
                    remove_columns=columns,
                    num_proc=4,
                )
            else:
                dataset = dataset.map(
                    preprocess_function,
                    batched=True,
                    remove_columns=columns,
                    num_proc=4,
                )

        # Remove unused columns.
        dataset = dataset.remove_columns(
            [col for col in columns if col not in ["input", "output"]]
        )

        if "train" not in dataset:
            dataset = DatasetDict({"train": dataset})

        return dataset

    # Load dataset.
    print("loading dataset...")
    dataset = load_data()

    print("formatting dataset...")
    add_prompts = True
    if args.training_type == "bonito_training" or args.supervision_source == "dapt":
        add_prompts = False

    dataset = format_dataset(dataset, add_prompts=add_prompts)
    eval_dataset = None

    if (args.do_eval or args.do_predict) or args.training_type == "bonito_training":
        if "eval" in dataset:
            eval_dataset = dataset["eval"]
        elif "validation" in dataset:
            eval_dataset = dataset["validation"]
        else:
            print(
                "Splitting train dataset in train and validation according to `eval_dataset_size`"
            )
            dataset = dataset["train"].train_test_split(
                test_size=args.eval_dataset_size, shuffle=True, seed=42
            )
            eval_dataset = dataset["test"]
            if (
                args.max_eval_samples is not None
                and len(eval_dataset) > args.max_eval_samples
            ):
                eval_dataset = eval_dataset.shuffle().select(
                    range(args.max_eval_samples)
                )
            if args.group_by_length:
                eval_dataset = eval_dataset.map(
                    lambda x: {"length": len(x["input"]) + len(x["output"])}
                )

    if args.do_train:
        train_dataset = dataset["train"]
        if (
            args.max_train_samples is not None
            and len(train_dataset) > args.max_train_samples
        ):
            train_dataset = train_dataset.select(range(args.max_train_samples))
        if args.group_by_length:
            train_dataset = train_dataset.map(
                lambda x: {"length": len(x["input"]) + len(x["output"])}
            )

    seq_collator = DataCollatorForCausalLM(
        tokenizer=tokenizer,
        source_max_len=args.source_max_len,
        target_max_len=args.target_max_len,
        train_on_source=args.train_on_source,
        predict_with_generate=args.predict_with_generate,
        model_name_or_path=args.model_name_or_path,
    )

    return {
        "train_dataset": train_dataset,
        "eval_dataset": eval_dataset,
        "seq_collator": seq_collator,
    }


def train():
    hfparser = transformers.HfArgumentParser(
        (ModelArguments, DataArguments, TrainingArguments)
    )
    (
        model_args,
        data_args,
        training_args,
        extra_args,
    ) = hfparser.parse_args_into_dataclasses(return_remaining_strings=True)

    args = argparse.Namespace(
        **vars(model_args), **vars(data_args), **vars(training_args)
    )
    print(args)

    checkpoint_model_id_or_path = args.checkpoint_model_id_or_path
    model, tokenizer = get_accelerate_model(args, checkpoint_model_id_or_path)

    model.config.use_cache = False
    print("loaded model")
    set_seed(args.seed)

    data_module = make_data_module(tokenizer=tokenizer, args=args)

    #
    if (
        args.training_type == "instruct"
        and not training_args.num_train_epochs
        and not training_args.max_steps
    ):
        if len(data_module["train_dataset"]) < 160000:
            training_args.num_train_epochs = 1
            training_args.save_strategy = "epoch"
            training_args.max_steps = -1

        else:
            training_args.max_steps = 10000
            training_args.save_strategy = "steps"
            training_args.save_steps = 10000

    trainer = Seq2SeqTrainer(
        model=model,
        tokenizer=tokenizer,
        args=training_args,
        train_dataset=data_module["train_dataset"],
        eval_dataset=data_module["eval_dataset"] if training_args.do_eval else None,
        data_collator=data_module["seq_collator"],
    )

    # Callbacks
    if not args.full_finetune:
        trainer.add_callback(SavePeftModelCallback)

    # Verifying the datatypes and parameter counts before training.
    print_trainable_parameters(args, model)
    dtypes = {}
    for _, p in model.named_parameters():
        dtype = p.dtype
        if dtype not in dtypes:
            dtypes[dtype] = 0
        dtypes[dtype] += p.numel()
    total = 0
    for k, v in dtypes.items():
        total += v
    for k, v in dtypes.items():
        print(k, v, v / total)

    all_metrics = {"run_name": args.run_name}

    # Training
    if args.do_train:
        if training_args.do_eval:
            trainer.evaluate(
                eval_dataset=data_module["eval_dataset"],
            )

        logger.info("*** Train ***")
        train_result = trainer.train()
        metrics = train_result.metrics
        trainer.log_metrics("train", metrics)
        trainer.save_metrics("train", metrics)
        trainer.save_state()
        all_metrics.update(metrics)


if __name__ == "__main__":
    train()
