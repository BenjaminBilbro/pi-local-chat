import json
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from collections import Counter

# Load stats from examine_session.py output
def load_session_stats(path):
    records = []
    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records

def compute_stats(records):
    stats = {}
    
    # Main agent stats
    main_assistant_msgs = []
    main_tool_calls = Counter()
    main_failed_tools = Counter()
    main_file_reads = Counter()
    main_reasoning_lengths = []
    main_wait_count = 0
    main_actually_count = 0
    main_last_tokens = 0
    
    subagent_stats = []
    
    for rec in records:
        if rec.get('type') != 'message':
            continue
        msg = rec.get('message', {})
        role = msg.get('role')
        
        if role == 'assistant':
            main_assistant_msgs.append(msg)
            main_last_tokens = msg.get('usage', {}).get('totalTokens', 0)
            
            for c in msg.get('content', []):
                if c.get('type') == 'toolCall':
                    main_tool_calls[c.get('name', 'unknown')] += 1
                    if c.get('name') == 'read':
                        path_arg = c.get('arguments', {}).get('path', '')
                        main_file_reads[path_arg] += 1
                if c.get('type') == 'thinking':
                    thinking_text = c.get('thinking', '').lower()
                    main_reasoning_lengths.append(len(c.get('thinking', '')))
                    main_wait_count += thinking_text.count('wait')
                    main_actually_count += thinking_text.count('actually')
        
        elif role == 'toolResult':
            if msg.get('isError'):
                main_failed_tools[msg.get('toolName', 'unknown')] += 1
            
            # Check for subagent
            if msg.get('toolName') == 'subagent':
                details = msg.get('details', {})
                results = details.get('results', [])
                for r in results:
                    sa_msgs = r.get('messages', [])
                    sa_usage = r.get('usage', {})
                    
                    sa_tool_calls = Counter()
                    sa_failed_tools = Counter()
                    sa_file_reads = Counter()
                    sa_reasoning_lengths = []
                    sa_wait_count = 0
                    sa_actually_count = 0
                    sa_assistant_count = 0
                    
                    for m in sa_msgs:
                        if m.get('role') == 'assistant':
                            sa_assistant_count += 1
                            for c in m.get('content', []):
                                if c.get('type') == 'toolCall':
                                    sa_tool_calls[c.get('name', 'unknown')] += 1
                                    if c.get('name') == 'read':
                                        path_arg = c.get('arguments', {}).get('path', '')
                                        sa_file_reads[path_arg] += 1
                                if c.get('type') == 'thinking':
                                    thinking_text = c.get('thinking', '').lower()
                                    sa_reasoning_lengths.append(len(c.get('thinking', '')))
                                    sa_wait_count += thinking_text.count('wait')
                                    sa_actually_count += thinking_text.count('actually')
                        elif m.get('role') == 'toolResult' and m.get('isError'):
                            sa_failed_tools[m.get('toolName', 'unknown')] += 1
                    
                    subagent_stats.append({
                        'tool_calls': sa_tool_calls,
                        'failed_tools': sa_failed_tools,
                        'file_reads': sa_file_reads,
                        'reasoning_lengths': sa_reasoning_lengths,
                        'wait_count': sa_wait_count,
                        'actually_count': sa_actually_count,
                        'assistant_count': sa_assistant_count,
                    })
    
    # Combine main + subagent stats
    combined_tool_calls = Counter(main_tool_calls)
    combined_failed_tools = Counter(main_failed_tools)
    combined_file_reads = Counter(main_file_reads)
    combined_reasoning = list(main_reasoning_lengths)
    combined_wait = main_wait_count
    combined_actually = main_actually_count
    combined_assistant = len(main_assistant_msgs)
    
    for sa in subagent_stats:
        combined_tool_calls.update(sa['tool_calls'])
        combined_failed_tools.update(sa['failed_tools'])
        combined_file_reads.update(sa['file_reads'])
        combined_reasoning.extend(sa['reasoning_lengths'])
        combined_wait += sa['wait_count']
        combined_actually += sa['actually_count']
        combined_assistant += sa['assistant_count']
    
    stats['total_tokens'] = main_last_tokens
    stats['total_assistant_msgs'] = combined_assistant
    stats['total_tool_calls'] = sum(combined_tool_calls.values())
    stats['total_failed_tools'] = sum(combined_failed_tools.values())
    stats['unique_files_read'] = len(combined_file_reads)
    stats['avg_reasoning_length'] = sum(combined_reasoning) / len(combined_reasoning) if combined_reasoning else 0
    stats['wait_actually_count'] = combined_wait + combined_actually
    stats['tool_call_distribution'] = combined_tool_calls
    
    return stats

# Load both sessions
session1_path = '/home/bbilbro/.pi/agent/sessions/--home-bbilbro-pi-chat--/2026-07-28T22-45-22-429Z_019faae7-717d-7221-bd0a-5fc186d57f7e.jsonl'
session2_path = '/home/bbilbro/.pi/agent/sessions/--home-bbilbro-pi-chat--/2026-07-28T21-59-53-603Z_019faabd-ce03-7bb1-85df-c93f7ecdb76d.jsonl'

records1 = load_session_stats(session1_path)
records2 = load_session_stats(session2_path)

stats1 = compute_stats(records1)
stats2 = compute_stats(records2)

# Color scheme
session1_color = '#3b82f6'  # blue
session2_color = '#ef4444'  # red

# Plot 1: Key metrics comparison
fig, ax = plt.subplots(figsize=(14, 7))

metrics = [
    'Num Tool Calls',
    'Num Tools Failed',
    'Num Files Read',
    'Avg Reasoning Length',
    'Total Tokens',
    'Total Assistant Msgs',
    'Num Wait/Actually',
]

values1 = [
    stats1['total_tool_calls'],
    stats1['total_failed_tools'],
    stats1['unique_files_read'],
    stats1['avg_reasoning_length'],
    stats1['total_tokens'],
    stats1['total_assistant_msgs'],
    stats1['wait_actually_count'],
]

values2 = [
    stats2['total_tool_calls'],
    stats2['total_failed_tools'],
    stats2['unique_files_read'],
    stats2['avg_reasoning_length'],
    stats2['total_tokens'],
    stats2['total_assistant_msgs'],
    stats2['wait_actually_count'],
]

# These metrics are on very different scales, so use secondary axes
x = range(len(metrics))
width = 0.35

# Normalize values for display on shared axis
# We'll use log scale or multiple axes
# Actually, let's use a log scale for better visibility
import numpy as np

bars1 = ax.bar([i - width/2 for i in x], values1, width, label='Session 1', color=session1_color)
bars2 = ax.bar([i + width/2 for i in x], values2, width, label='Session 2', color=session2_color)

ax.set_yscale('log')
ax.set_ylabel('Value (log scale)')
ax.set_title('Session Comparison: Key Metrics', fontsize=14, fontweight='bold')
ax.set_xticks(x)
ax.set_xticklabels(metrics, rotation=45, ha='right')
ax.legend(loc='upper left')
ax.grid(axis='y', alpha=0.3)

# Add value labels on bars
def add_labels(bars):
    for bar in bars:
        height = bar.get_height()
        ax.annotate(f'{height:.0f}',
                    xy=(bar.get_x() + bar.get_width() / 2, height),
                    xytext=(0, 3),
                    textcoords="offset points",
                    ha='center', va='bottom', fontsize=7)

add_labels(bars1)
add_labels(bars2)

plt.tight_layout()
plt.savefig('/home/bbilbro/pi-chat/session_comparison_metrics.png', dpi=150, bbox_inches='tight')
plt.close()

# Plot 2: Tool type distribution
fig, axes = plt.subplots(1, 2, figsize=(14, 6))

# Session 1
tools1 = stats1['tool_call_distribution']
sorted_tools1 = sorted(tools1.items(), key=lambda x: -x[1])
axes[0].barh([t[0] for t in sorted_tools1], [t[1] for t in sorted_tools1], color=session1_color)
axes[0].set_xlabel('Number of Calls')
axes[0].set_title('Session 1: Tool Distribution', fontsize=12, fontweight='bold')
axes[0].grid(axis='x', alpha=0.3)

# Add value labels
for i, (tool, count) in enumerate(sorted_tools1):
    axes[0].annotate(str(count), xy=(count, i), xytext=(5, 0), textcoords='offset points', va='center', fontsize=9)

# Session 2
tools2 = stats2['tool_call_distribution']
sorted_tools2 = sorted(tools2.items(), key=lambda x: -x[1])
axes[1].barh([t[0] for t in sorted_tools2], [t[1] for t in sorted_tools2], color=session2_color)
axes[1].set_xlabel('Number of Calls')
axes[1].set_title('Session 2: Tool Distribution', fontsize=12, fontweight='bold')
axes[1].grid(axis='x', alpha=0.3)

# Add value labels
for i, (tool, count) in enumerate(sorted_tools2):
    axes[1].annotate(str(count), xy=(count, i), xytext=(5, 0), textcoords='offset points', va='center', fontsize=9)

plt.suptitle('Tool Type Distribution by Session', fontsize=14, fontweight='bold', y=1.02)
plt.tight_layout()
plt.savefig('/home/bbilbro/pi-chat/session_tool_distribution.png', dpi=150, bbox_inches='tight')
plt.close()

print("Visualizations saved:")
print("  - /home/bbilbro/pi-chat/session_comparison_metrics.png")
print("  - /home/bbilbro/pi-chat/session_tool_distribution.png")
