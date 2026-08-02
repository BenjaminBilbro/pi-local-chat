import json
from collections import Counter

def analyze_messages(messages):
    """Analyze a list of messages (top-level or subagent) and return stats.
    Note: total_tokens is the LAST message's cumulative totalTokens, not a sum."""
    tool_call_counts = Counter()
    file_reads = Counter()
    offset_reads = 0
    wait_count = 0
    actually_count = 0
    reasoning_lengths = []
    failed_tools = Counter()
    last_total_tokens = 0
    assistant_count = 0
    tool_result_count = 0

    for msg in messages:
        role = msg.get('role')

        if role == 'assistant':
            assistant_count += 1
            usage = msg.get('usage', {})
            # Use last message's cumulative totalTokens
            last_total_tokens = usage.get('totalTokens', 0)

            for c in msg.get('content', []):
                if c.get('type') == 'toolCall':
                    tool_call_counts[c.get('name', 'unknown')] += 1
                    if c.get('name') == 'read':
                        args = c.get('arguments', {})
                        path_arg = args.get('path', '')
                        file_reads[path_arg] += 1
                        if 'offset' in args:
                            offset_reads += 1

                if c.get('type') == 'thinking':
                    thinking_text = c.get('thinking', '').lower()
                    wait_count += thinking_text.count('wait')
                    actually_count += thinking_text.count('actually')
                    reasoning_lengths.append(len(c.get('thinking', '')))

        elif role == 'toolResult':
            tool_result_count += 1
            if msg.get('isError'):
                failed_tools[msg.get('toolName', 'unknown')] += 1

    return {
        'tool_call_counts': dict(tool_call_counts),
        'file_reads': dict(file_reads),
        'offset_reads': offset_reads,
        'wait_count': wait_count,
        'actually_count': actually_count,
        'reasoning_lengths': reasoning_lengths,
        'failed_tools': dict(failed_tools),
        'total_tokens': last_total_tokens,
        'assistant_count': assistant_count,
        'tool_result_count': tool_result_count,
    }


def find_main_agent_tokens_before_subagent(records, subagent_toolCall_id):
    """Find the main agent's cumulative totalTokens right before invoking the subagent."""
    for rec in records:
        if rec.get('type') != 'message':
            continue
        msg = rec.get('message', {})
        if msg.get('role') != 'assistant':
            continue
        for c in msg.get('content', []):
            if c.get('type') == 'toolCall' and c.get('name') == 'subagent' and c.get('id') == subagent_toolCall_id:
                return msg.get('usage', {}).get('totalTokens', 0)
    return 0


def extract_subagent_stats(records):
    """Extract stats from subagent toolResult messages."""
    subagent_stats_list = []

    for rec in records:
        if rec.get('type') != 'message':
            continue
        msg = rec.get('message', {})
        if msg.get('role') == 'toolResult' and msg.get('toolName') == 'subagent':
            toolCallId = msg.get('toolCallId')
            details = msg.get('details', {})
            results = details.get('results', [])
            for r in results:
                agent_name = r.get('agent', 'unknown')
                messages = r.get('messages', [])
                usage = r.get('usage', {})
                turns = usage.get('turns', 0)
                saw_agent_start = r.get('sawAgentStart', False)
                error_msg = r.get('errorMessage', '')
                
                # Subagent's actual token usage
                main_tokens_before = find_main_agent_tokens_before_subagent(records, toolCallId)
                subagent_context_tokens = usage.get('contextTokens', 0)
                
                if subagent_context_tokens > main_tokens_before:
                    # Normal case: contextTokens is cumulative including subagent work
                    subagent_actual_tokens = subagent_context_tokens - main_tokens_before
                elif subagent_context_tokens == 0:
                    # Aborted/incomplete subagent: fall back to input + output
                    subagent_actual_tokens = usage.get('input', 0) + usage.get('output', 0)
                else:
                    subagent_actual_tokens = 0

                stats = analyze_messages(messages)
                stats['agent_name'] = agent_name
                stats['turns'] = turns
                stats['saw_agent_start'] = saw_agent_start
                stats['error_message'] = error_msg
                stats['main_tokens_before_subagent'] = main_tokens_before
                stats['subagent_context_tokens'] = subagent_context_tokens
                stats['subagent_actual_tokens'] = subagent_actual_tokens
                subagent_stats_list.append(stats)

    return subagent_stats_list


def analyze_session(path):
    records = []
    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    # Get model
    model = None
    for rec in records:
        if rec.get('type') == 'model_change':
            model = rec.get('modelId')
            break

    # Analyze top-level messages
    top_level_messages = [
        rec.get('message') for rec in records
        if rec.get('type') == 'message'
    ]
    top_stats = analyze_messages(top_level_messages)

    # Extract subagent stats
    subagent_stats = extract_subagent_stats(records)

    # Combine stats
    combined_tool_calls = Counter(top_stats['tool_call_counts'])
    combined_file_reads = Counter(top_stats['file_reads'])
    combined_failed_tools = Counter(top_stats['failed_tools'])
    combined_reasoning = list(top_stats['reasoning_lengths'])

    for sa in subagent_stats:
        combined_tool_calls.update(sa['tool_call_counts'])
        combined_file_reads.update(sa['file_reads'])
        combined_failed_tools.update(sa['failed_tools'])
        combined_reasoning.extend(sa['reasoning_lengths'])

    # Token calculation:
    # - main_agent_tokens = last assistant message's totalTokens (cumulative)
    # - subagent_tokens = subagent_contextTokens - main_tokens_before_subagent
    # - total = main_agent_tokens + subagent_tokens
    main_agent_tokens = top_stats['total_tokens']  # last message's cumulative totalTokens
    total_subagent_tokens = sum(sa['subagent_actual_tokens'] for sa in subagent_stats)
    combined_total_tokens = main_agent_tokens + total_subagent_tokens

    unique_files = len(combined_file_reads)
    files_read_multiple = {p: c for p, c in combined_file_reads.items() if c > 1}

    # User interventions (user messages after the first)
    user_messages = []
    for rec in records:
        if rec.get('type') != 'message':
            continue
        msg = rec.get('message', {})
        if msg.get('role') == 'user':
            content = msg.get('content', [])
            text_parts = [c.get('text', '') for c in content if c.get('type') == 'text']
            user_messages.append(' '.join(text_parts))

    interventions = user_messages[1:] if len(user_messages) > 1 else []

    return {
        'model': model,
        'top_level': top_stats,
        'subagents': subagent_stats,
        'combined_tool_call_counts': dict(combined_tool_calls),
        'unique_files_read': unique_files,
        'files_read_multiple': files_read_multiple,
        'offset_reads': top_stats['offset_reads'] + sum(sa['offset_reads'] for sa in subagent_stats),
        'wait_count': top_stats['wait_count'] + sum(sa['wait_count'] for sa in subagent_stats),
        'actually_count': top_stats['actually_count'] + sum(sa['actually_count'] for sa in subagent_stats),
        'reasoning_lengths': combined_reasoning,
        'failed_tools': dict(combined_failed_tools),
        'total_tokens': combined_total_tokens,
        'user_interventions': interventions,
        'total_assistant_messages': top_stats['assistant_count'] + sum(sa['assistant_count'] for sa in subagent_stats),
        'total_tool_results': top_stats['tool_result_count'] + sum(sa['tool_result_count'] for sa in subagent_stats),
    }


# Session paths
session1_path = '/home/bbilbro/.pi/agent/sessions/--home-bbilbro-pi-chat--/2026-07-28T22-45-22-429Z_019faae7-717d-7221-bd0a-5fc186d57f7e.jsonl'
session2_path = '/home/bbilbro/.pi/agent/sessions/--home-bbilbro-pi-chat--/2026-07-28T21-59-53-603Z_019faabd-ce03-7bb1-85df-c93f7ecdb76d.jsonl'

# Analyze both sessions
stats1 = analyze_session(session1_path)
stats2 = analyze_session(session2_path)


def print_stats(name, stats):
    print(f"\n{'='*60}")
    print(f"SESSION: {name}")
    print(f"Model: {stats['model']}")
    print(f"{'='*60}")

    # Top-level stats
    top = stats['top_level']
    print(f"\n--- Top-Level Agent ---")
    print(f"  Assistant messages: {top['assistant_count']}")
    print(f"  Tool results: {top['tool_result_count']}")
    print(f"  Tokens (cumulative, last message): {top['total_tokens']}")
    print(f"  Tool calls:")
    for tool, count in sorted(top['tool_call_counts'].items(), key=lambda x: -x[1]):
        print(f"    {tool}: {count}")

    # Subagent stats
    subagents = stats['subagents']
    if subagents:
        print(f"\n--- Subagents ({len(subagents)}) ---")
        for sa in subagents:
            print(f"\n  Agent: {sa['agent_name']}")
            print(f"    sawAgentStart: {sa['saw_agent_start']}, turns: {sa['turns']}")
            if sa['error_message']:
                print(f"    Error: {sa['error_message'][:150]}")
            print(f"    Assistant messages: {sa['assistant_count']}, Tool results: {sa['tool_result_count']}")
            print(f"    Main agent tokens before subagent: {sa['main_tokens_before_subagent']}")
            print(f"    Subagent contextTokens: {sa['subagent_context_tokens']}")
            print(f"    Subagent actual tokens: {sa['subagent_actual_tokens']}")
            print(f"    Tool calls:")
            for tool, count in sorted(sa['tool_call_counts'].items(), key=lambda x: -x[1]):
                print(f"      {tool}: {count}")
            print(f"    Files read: {len(sa['file_reads'])} unique")
            print(f"    'wait': {sa['wait_count']}, 'actually': {sa['actually_count']}")
            if sa['failed_tools']:
                print(f"    Failed tools: {sa['failed_tools']}")

    # Combined stats
    total_subagent_tokens = sum(sa['subagent_actual_tokens'] for sa in subagents)
    print(f"\n--- COMBINED (top-level + subagents) ---")
    print(f"  Assistant messages: {stats['total_assistant_messages']}")
    print(f"  Tool results: {stats['total_tool_results']}")
    print(f"  Main agent tokens: {stats['top_level']['total_tokens']}")
    print(f"  Subagent tokens: {total_subagent_tokens}")
    print(f"  Total tokens: {stats['total_tokens']}")

    print(f"\n  Tool calls:")
    for tool, count in sorted(stats['combined_tool_call_counts'].items(), key=lambda x: -x[1]):
        print(f"    {tool}: {count}")

    print(f"\n  Files read:")
    print(f"    Unique files: {stats['unique_files_read']}")
    print(f"    Offset reads (partial): {stats['offset_reads']}")
    if stats['files_read_multiple']:
        print(f"    Files read multiple times:")
        for path, count in sorted(stats['files_read_multiple'].items(), key=lambda x: -x[1]):
            print(f"      {path}: {count} times")

    print(f"\n  Reasoning:")
    print(f"    'wait' occurrences: {stats['wait_count']}")
    print(f"    'actually' occurrences: {stats['actually_count']}")
    lengths = stats['reasoning_lengths']
    if lengths:
        print(f"    Thinking blocks: {len(lengths)}")
        print(f"    Total reasoning chars: {sum(lengths)}")
        print(f"    Avg reasoning chars: {sum(lengths)/len(lengths):.0f}")
        print(f"    Max reasoning chars: {max(lengths)}")
        print(f"    Min reasoning chars: {min(lengths)}")

    print(f"\n  Failed tool calls:")
    if stats['failed_tools']:
        for tool, count in stats['failed_tools'].items():
            print(f"    {tool}: {count}")
    else:
        print(f"    None")

    print(f"\n  User interventions: {len(stats['user_interventions'])}")
    for i, intervention in enumerate(stats['user_interventions'], 1):
        print(f"    Intervention {i}: {intervention[:200]}...")


print_stats("Session 1", stats1)
print_stats("Session 2", stats2)

# Save reasoning lengths for statistical analysis
with open('/home/bbilbro/pi-chat/session1_reasoning_lengths.json', 'w') as f:
    json.dump(stats1['reasoning_lengths'], f, indent=2)
with open('/home/bbilbro/pi-chat/session2_reasoning_lengths.json', 'w') as f:
    json.dump(stats2['reasoning_lengths'], f, indent=2)
print("\nReasoning lengths saved to session1_reasoning_lengths.json and session2_reasoning_lengths.json")