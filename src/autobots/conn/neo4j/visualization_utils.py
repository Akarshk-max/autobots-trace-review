import json
from typing import Any, Dict, List, Optional

# Complete HTML template with perfectly centered collapse button
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<title>Knowledge Graph Explorer</title>
<script src="https://unpkg.com/vis-network@9.1.2/standalone/umd/vis-network.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
<style>
    :root {{
        /* Kiwi Autobots Design System - Light Mode */
        --bg-primary: #ffffff;
        --bg-secondary: #f6f8fa;
        --bg-tertiary: #f0f2f5;
        --text-primary: #1f2328;
        --text-secondary: #57606a;
        --text-muted: #6e7681;
        --border-primary: #d0d7de;
        --border-secondary: #e1e4e8;
        --accent-primary: #20C997;
        --accent-primary-hover: #1a9d7a;
        --accent-secondary: #0969da;
        --accent-secondary-hover: #0550ae;
        --success-emphasis: #238636;
        --info-emphasis: #0969da;
        --shadow-default: rgba(0, 0, 0, 0.1);
        --shadow-tooltip: rgba(0, 0, 0, 0.15);
        
        /* Professional design tokens */
        --font-size-xl: 20px;
        --font-size-lg: 16px;
        --font-size-md: 14px;
        --font-size-sm: 12px;
        --font-size-xs: 11px;
        --space-xs: 4px;
        --space-sm: 8px;
        --space-md: 16px;
        --space-lg: 24px;
        --space-xl: 32px;
        --line-height-tight: 1.25;
        --line-height-normal: 1.5;
        --line-height-relaxed: 1.75;
    }}

    [data-theme="dark"] {{
        /* Kiwi Autobots Design System - Dark Mode */
        --bg-primary: #0d1117;
        --bg-secondary: #161b22;
        --bg-tertiary: #21262d;
        --text-primary: #f0f6fc;
        --text-secondary: #8b949e;
        --text-muted: #6e7681;
        --border-primary: #30363d;
        --border-secondary: #21262d;
        --accent-primary: #20C997;
        --accent-primary-hover: #4dd4ac;
        --accent-secondary: #1f6feb;
        --accent-secondary-hover: #388bfd;
        --success-emphasis: #238636;
        --info-emphasis: #1f6feb;
        --shadow-default: rgba(0, 0, 0, 0.3);
        --shadow-tooltip: rgba(0, 0, 0, 0.5);
    }}

    * {{
        box-sizing: border-box;
        transition: background-color 0.3s ease, border-color 0.3s ease, color 0.3s ease;
    }}

    body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif;
        margin: 0;
        background: var(--bg-primary);
        color: var(--text-primary);
        overflow: hidden;
        line-height: var(--line-height-normal);
    }}

    #header {{
        background: var(--bg-secondary);
        padding: var(--space-md) var(--space-lg);
        border-bottom: 1px solid var(--border-primary);
        display: flex;
        justify-content: space-between;
        align-items: center;
        z-index: 100;
        position: relative;
        box-shadow: 0 1px 3px var(--shadow-default);
    }}

    #header h1 {{
        margin: 0;
        font-size: var(--font-size-xl);
        font-weight: 600;
        display: flex;
        align-items: center;
        gap: var(--space-sm);
        color: var(--text-primary);
    }}

    .logo-icon {{
        width: 24px;
        height: 24px;
        fill: var(--accent-primary);
    }}

    #controls {{
        display: flex;
        gap: var(--space-sm);
        align-items: center;
    }}

    .control-btn {{
        padding: var(--space-sm) var(--space-md);
        border: 1px solid var(--border-primary);
        border-radius: 6px;
        background: var(--bg-tertiary);
        color: var(--text-primary);
        cursor: pointer;
        transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
        font-size: var(--font-size-md);
        font-weight: 500;
        display: flex;
        align-items: center;
        gap: 6px;
        min-height: 44px;
    }}

    .control-btn:hover {{
        background: var(--accent-primary);
        border-color: var(--accent-primary);
        color: white;
        transform: translateY(-1px);
        box-shadow: 0 4px 8px rgba(32, 201, 151, 0.3);
    }}

    .control-btn.active {{
        background: var(--accent-primary);
        border-color: var(--accent-primary);
        color: white;
    }}

    /* Theme Switcher */
    .theme-switcher {{
        position: relative;
        width: 60px;
        height: 30px;
        background: var(--border-primary);
        border-radius: 15px;
        cursor: pointer;
        transition: all 0.3s ease;
        border: 1px solid var(--border-primary);
        margin-left: var(--space-sm);
    }}

    .theme-switcher:hover {{
        background: var(--border-secondary);
    }}

    .theme-switcher .switch-handle {{
        position: absolute;
        top: 2px;
        left: 2px;
        width: 24px;
        height: 24px;
        background: var(--bg-primary);
        border-radius: 50%;
        transition: all 0.3s ease;
        display: flex;
        align-items: center;
        justify-content: center;
        box-shadow: 0 2px 4px var(--shadow-default);
    }}

    .theme-switcher .switch-icon {{
        width: 14px;
        height: 14px;
        fill: var(--text-secondary);
        transition: all 0.3s ease;
    }}

    [data-theme="dark"] .theme-switcher {{
        background: var(--accent-primary);
    }}

    [data-theme="dark"] .theme-switcher .switch-handle {{
        transform: translateX(30px);
        background: var(--bg-primary);
    }}

    [data-theme="dark"] .theme-switcher .switch-icon {{
        fill: var(--accent-primary);
    }}

    .theme-switcher-label {{
        font-size: var(--font-size-xs);
        color: var(--text-muted);
        margin-right: var(--space-sm);
        user-select: none;
    }}

    #network-container {{
        position: absolute;
        top: 73px;
        left: 0;
        right: 380px;
        bottom: 0;
        background: var(--bg-primary);
        transition: all 0.3s ease;
        z-index: 1;
    }}

    #info-panel {{
        position: absolute;
        top: 73px;
        right: 0;
        width: 380px;
        bottom: 0;
        background: var(--bg-secondary);
        border-left: 1px solid var(--border-primary);
        display: flex;
        flex-direction: column;
        overflow: hidden;
        transition: transform 0.3s ease;
        z-index: 10;
    }}

    /* Muted circular collapse button */
    .sidebar-collapse-btn {{
        position: fixed;
        width: 36px;
        height: 36px;
        background: var(--bg-primary);
        border: 1px solid var(--border-primary);
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        cursor: pointer;
        z-index: 1000;
        transition: background-color 0.2s ease, border-color 0.2s ease, color 0.2s ease, box-shadow 0.2s ease;
        color: var(--text-secondary);
        box-shadow: 0 2px 8px var(--shadow-default);
    }}

    .sidebar-collapse-btn:hover {{
        background: var(--bg-tertiary);
        border-color: var(--border-secondary);
        color: var(--text-primary);
        box-shadow: 0 4px 12px var(--shadow-default);
    }}

    .sidebar-collapse-btn i {{
        font-size: 14px;
        transition: transform 0.3s ease;
    }}

    .panel-content {{
        flex: 1;
        overflow-y: auto;
        padding: var(--space-lg);
    }}

    .info-section {{
        margin-bottom: var(--space-lg);
        border: 1px solid var(--border-primary);
        border-radius: 8px;
        background: var(--bg-tertiary);
        overflow: hidden;
    }}

    .section-header {{
        padding: var(--space-md) var(--space-lg);
        background: var(--bg-secondary);
        border-bottom: 1px solid var(--border-primary);
    }}

    .section-header h3 {{
        margin: 0;
        font-size: var(--font-size-md);
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: var(--text-secondary);
    }}

    .section-content {{
        padding: var(--space-lg);
    }}

    /* Enhanced Stats */
    .stats-compact {{
        display: flex;
        gap: var(--space-sm);
        justify-content: space-between;
    }}

    .stat-item {{
        text-align: center;
        padding: var(--space-sm);
        background: var(--bg-primary);
        border-radius: 6px;
        border: 1px solid var(--border-secondary);
        flex: 1;
        transition: all 0.2s ease;
        cursor: default;
        min-height: 44px;
        display: flex;
        flex-direction: column;
        justify-content: center;
    }}

    .stat-item:hover {{
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(32, 201, 151, 0.1);
    }}

    .stat-number {{
        font-size: 24px;
        font-weight: 700;
        color: #1a9d7a;
        margin-bottom: 4px;
        display: block;
        line-height: var(--line-height-tight);
    }}

    .stat-label {{
        font-size: var(--font-size-xs);
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: 0.5px;
        width: 100%;
        text-align: center;
    }}

    /* Enhanced Property Display */
    .property-display {{
        background: var(--bg-primary);
        border-radius: 8px;
        border: 1px solid var(--border-secondary);
        overflow: hidden;
    }}

    .property-section {{
        border-bottom: 1px solid var(--border-secondary);
    }}

    .property-section:last-child {{
        border-bottom: none;
    }}

    .property-section-header {{
        padding: var(--space-sm) var(--space-md);
        background: var(--bg-secondary);
        font-weight: 600;
        font-size: var(--font-size-sm);
        color: var(--text-primary);
        border-bottom: 1px solid var(--border-secondary);
    }}

    .property-section-content {{
        padding: var(--space-md);
    }}

    .property-grid {{
        display: grid;
        gap: var(--space-sm);
    }}

    .property-item {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: var(--space-sm);
        border-bottom: 1px solid var(--border-secondary);
        transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
        border-radius: 4px;
        margin: 0 -var(--space-sm);
        cursor: pointer;
        min-height: 44px;
    }}

    .property-item:last-child {{
        border-bottom: none;
    }}

    .property-item:hover,
    .property-item:focus {{
        background: var(--bg-tertiary);
        transform: translateX(2px);
    }}

    /* Touch feedback for mobile */
    @media (hover: none) {{
        .property-item:active {{
            background: var(--accent-primary);
            color: white;
            transform: scale(0.98);
        }}
    }}

    .property-label {{
        font-weight: 500;
        color: var(--text-secondary);
        font-size: var(--font-size-xs);
        text-transform: uppercase;
        letter-spacing: 0.5px;
        min-width: 80px;
        margin-right: var(--space-sm);
    }}

    .property-value {{
        color: var(--text-primary);
        font-size: var(--font-size-sm);
        word-break: break-word;
        flex: 1;
        text-align: right;
        line-height: var(--line-height-normal);
    }}

    .property-value.text-content {{
        background: var(--bg-tertiary);
        padding: var(--space-sm);
        border-radius: 4px;
        font-family: 'SF Mono', Monaco, Inconsolata, 'Roboto Mono', Consolas, 'Courier New', monospace;
        font-size: var(--font-size-xs);
        line-height: var(--line-height-relaxed);
        white-space: pre-wrap;
        text-align: left;
        margin-top: var(--space-xs);
        max-height: 120px;
        overflow-y: auto;
    }}

    .property-value.speaker {{
        display: flex;
        align-items: center;
        gap: 6px;
        justify-content: flex-end;
    }}

    .property-value.speaker::before {{
        content: attr(data-speaker-icon);
        font-size: var(--font-size-md);
    }}

    /* Enhanced relationship display */
    .relationship-summary {{
        background: var(--bg-tertiary);
        border: 1px solid var(--border-primary);
        border-radius: 8px;
        padding: var(--space-lg);
    }}

    .relationship-vertical {{
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: var(--space-md);
        font-weight: 500;
    }}

    .relationship-node-full {{
        padding: var(--space-md);
        background: var(--bg-primary);
        border: 1px solid var(--border-primary);
        border-radius: 6px;
        font-size: var(--font-size-md);
        color: var(--text-primary);
        text-align: center;
        width: 100%;
        word-break: break-word;
        font-weight: 500;
        line-height: var(--line-height-normal);
    }}

    .relationship-arrow-vertical {{
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: var(--space-sm);
        color: var(--accent-primary);
    }}

    .relationship-label {{
        background: var(--accent-primary);
        color: white;
        padding: 6px var(--space-sm);
        border-radius: 4px;
        font-size: var(--font-size-sm);
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }}

    /* Professional data formatting */
    .tag {{
        display: inline-block;
        padding: 2px 6px;
        background: var(--accent-primary);
        color: white;
        border-radius: 10px;
        font-size: var(--font-size-xs);
        margin: 2px;
        font-weight: 500;
    }}

    .data-link {{
        color: var(--accent-primary);
        text-decoration: none;
        border-bottom: 1px dotted var(--accent-primary);
    }}

    .data-link:hover {{
        border-bottom-style: solid;
    }}

    .empty-state {{
        text-align: center;
        padding: var(--space-xl) var(--space-lg);
        color: var(--text-muted);
    }}

    .empty-state-icon {{
        width: 48px;
        height: 48px;
        margin: 0 auto var(--space-md);
        opacity: 0.5;
        fill: var(--text-muted);
    }}

    /* Custom tooltip styles */
    .custom-tooltip {{
        position: absolute;
        background: var(--bg-tertiary);
        color: var(--text-primary);
        padding: var(--space-sm) var(--space-md);
        border-radius: 8px;
        border: 1px solid var(--border-primary);
        box-shadow: 0 8px 24px var(--shadow-tooltip);
        font-size: var(--font-size-sm);
        line-height: var(--line-height-normal);
        max-width: 320px;
        z-index: 1000;
        pointer-events: none;
        opacity: 0;
        transition: all 0.2s ease;
        backdrop-filter: blur(8px);
    }}

    .custom-tooltip.visible {{
        opacity: 1;
    }}

    .custom-tooltip .tooltip-title {{
        font-weight: 600;
        color: var(--text-primary);
        margin-bottom: var(--space-sm);
        font-size: var(--font-size-md);
    }}

    .custom-tooltip .tooltip-content {{
        color: var(--text-secondary);
        font-size: var(--font-size-sm);
    }}

    .custom-tooltip .tooltip-type {{
        display: inline-block;
        padding: 2px var(--space-sm);
        border-radius: 12px;
        font-size: var(--font-size-xs);
        font-weight: 500;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin-bottom: var(--space-sm);
    }}

    .custom-tooltip .tooltip-type.chunk {{
        background: rgba(32, 201, 151, 0.15);
        color: var(--accent-primary);
        border: 1px solid rgba(32, 201, 151, 0.3);
    }}

    .custom-tooltip .tooltip-type.entity {{
        background: rgba(9, 105, 218, 0.15);
        color: var(--accent-secondary);
        border: 1px solid rgba(9, 105, 218, 0.3);
    }}

    .custom-tooltip .tooltip-type.document {{
        background: rgba(111, 66, 193, 0.15);
        color: #6f42c1;
        border: 1px solid rgba(111, 66, 193, 0.3);
    }}

    .custom-tooltip .tooltip-meta {{
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: var(--font-size-xs);
        color: var(--text-muted);
        margin-bottom: var(--space-xs);
    }}

    /* Mobile responsive design */
    @media (max-width: 768px) {{
        #network-container {{
            right: 0;
            bottom: 0;
        }}
        
        #network-container.panel-expanded {{
            bottom: 40%;
        }}
        
        #info-panel {{
            width: 100%;
            height: 40%;
            top: auto;
            bottom: 0;
            border-left: none;
            border-top: 1px solid var(--border-primary);
            transform: translateY(calc(100% - 60px));
        }}
        
        #info-panel.expanded {{
            transform: translateY(0);
        }}
        
        #header {{
            padding: var(--space-md);
        }}
        
        #header h1 {{
            font-size: var(--font-size-lg);
        }}
        
        .control-btn {{
            padding: var(--space-sm);
            font-size: var(--font-size-sm);
        }}
    }}

    /* Desktop responsive design */
    @media (min-width: 769px) {{
        #network-container.sidebar-collapsed {{
            right: 0;
        }}
        
        #info-panel.collapsed {{
            transform: translateX(100%);
        }}
    }}

    /* Custom scrollbar */
    ::-webkit-scrollbar {{
        width: 8px;
    }}

    ::-webkit-scrollbar-track {{
        background: var(--bg-secondary);
    }}

    ::-webkit-scrollbar-thumb {{
        background: var(--border-primary);
        border-radius: 4px;
    }}

    ::-webkit-scrollbar-thumb:hover {{
        background: var(--text-muted);
    }}
</style>
</head>
<body>

<div id="custom-tooltip" class="custom-tooltip"></div>

<div id="header">
    <h1>
        <svg class="logo-icon" viewBox="0 0 24 24">
            <path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>
        </svg>
        Knowledge Graph Explorer
    </h1>
    <div id="controls">
        <button class="control-btn" id="physics-btn" onclick="togglePhysics()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/>
            </svg>
            Physics
        </button>
        <button class="control-btn" onclick="fitNetwork()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <path d="M9 11H7v6h2v-6zm4 0h-2v6h2v-6zm4 0h-2v6h2v-6zm2.5-9H19V1h-1.5v1H6.5V1H5v1H4.5C3.67 2 3 2.67 3 3.5v16c0 .83.67 1.5 1.5 1.5h15c.83 0 1.5-.67 1.5-1.5v-16C21 2.67 20.33 2 19.5 2z"/>
            </svg>
            Fit View
        </button>
        
        <div style="display: flex; align-items: center; margin-left: var(--space-md);">
            <span class="theme-switcher-label">Theme</span>
            <div class="theme-switcher" onclick="toggleTheme()" title="Toggle light/dark theme">
                <div class="switch-handle">
                    <svg class="switch-icon sun-icon" viewBox="0 0 24 24" style="display: block;">
                        <path d="M12 7c-2.76 0-5 2.24-5 5s2.24 5 5 5 5-2.24 5-5-2.24-5-5-5zM2 13h2c.55 0 1-.45 1-1s-.45-1-1-1H2c-.55 0-1 .45-1 1s.45 1 1 1zm18 0h2c.55 0 1-.45 1-1s-.45-1-1-1h-2c-.55 0-1 .45-1 1s.45 1 1 1zM11 2v2c0 .55.45 1 1 1s1-.45 1-1V2c0-.55-.45-1-1-1s-1 .45-1 1zm0 18v2c0 .55.45 1 1 1s1-.45 1-1v-2c0-.55-.45-1-1-1s-1 .45-1 1zM5.99 4.58c-.39-.39-1.03-.39-1.41 0-.39.39-.39 1.03 0 1.41l1.06 1.06c.39.39 1.03.39 1.41 0s.39-1.03 0-1.41L5.99 4.58zm12.37 12.37c-.39-.39-1.03-.39-1.41 0-.39.39-.39 1.03 0 1.41l1.06 1.06c.39.39 1.03.39 1.41 0 .39-.39.39-1.03 0-1.41l-1.06-1.06zm1.06-10.96c.39-.39.39-1.03 0-1.41-.39-.39-1.03-.39-1.41 0l-1.06 1.06c-.39.39-.39 1.03 0 1.41s1.03.39 1.41 0l1.06-1.06zM7.05 18.36c.39-.39.39-1.03 0-1.41-.39-.39-1.03-.39-1.41 0l-1.06 1.06c-.39.39-.39 1.03 0 1.41s1.03.39 1.41 0l1.06-1.06z"/>
                    </svg>
                    <svg class="switch-icon moon-icon" viewBox="0 0 24 24" style="display: none;">
                        <path d="M12.34 2.02C6.59 1.82 2 6.42 2 12c0 5.52 4.48 10 10 10 3.71 0 6.93-2.02 8.66-5.02-7.51-.25-13.64-6.42-13.64-13.96 0-.34.02-.67.05-1z"/>
                    </svg>
                </div>
            </div>
        </div>
    </div>
</div>

<div id="network-container"></div>

<div class="sidebar-collapse-btn" id="sidebar-toggle" onclick="toggleSidebar()" title="Toggle sidebar">
    <i class="fas fa-chevron-right"></i>
</div>

<div id="info-panel">
    <div class="panel-content">
        <div class="info-section">
            <div class="section-header">
                <h3>Graph Overview</h3>
            </div>
            <div class="section-content">
                <div class="stats-compact">
                    <div class="stat-item">
                        <span class="stat-number" id="total-nodes">0</span>
                        <span class="stat-label">Nodes</span>
                    </div>
                    <div class="stat-item">
                        <span class="stat-number" id="total-edges">0</span>
                        <span class="stat-label">Edges</span>
                    </div>
                    <div class="stat-item">
                        <span class="stat-number" id="selected-count">0</span>
                        <span class="stat-label" id="selected-label">Selected</span>
                    </div>
                </div>
            </div>
        </div>

        <div class="info-section">
            <div class="section-header">
                <h3 id="selection-title">Selection Details</h3>
            </div>
            <div class="section-content">
                <div id="selection-content">
                    <div class="empty-state">
                        <svg class="empty-state-icon" viewBox="0 0 24 24">
                            <path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z"/>
                        </svg>
                        <p>Click a node or edge to view details</p>
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>

<script>
window.addEventListener('load', function() {{
    const graphData = {graph_data};
    let network;
    let physicsEnabled = true;
    let sidebarCollapsed = false;
    let currentLayoutMode = null; // Track layout mode changes
    const tooltip = document.getElementById('custom-tooltip');
    
    // Harmonized color palette based on Kiwi brand
    const HARMONIZED_COLORS = {{
        'Document': {{ bg: '#6366F1', border: '#4F46E5' }},
        'Chunk': {{ bg: '#20C997', border: '#1a9d7a' }},
        'Person': {{ bg: '#F59E0B', border: '#D97706' }},
        'Organization': {{ bg: '#3B82F6', border: '#2563EB' }},
        'Location': {{ bg: '#06B6D4', border: '#0891B2' }},
        'LegalTerm': {{ bg: '#8B5CF6', border: '#7C3AED' }},
        'Concept': {{ bg: '#10B981', border: '#059669' }}
    }};

    // Professional icons for different node types
    function getNodeIcon(nodeType) {{
        const icons = {{
            'Document': 'fas fa-file-alt',
            'Person': 'fas fa-user',
            'Organization': 'fas fa-building',
            'Location': 'fas fa-map-marker-alt',
            'LegalTerm': 'fas fa-gavel',
            'Concept': 'fas fa-lightbulb',
            'Chunk': 'fas fa-cube'
        }};
        return icons[nodeType] || 'fas fa-question-circle';
    }}

    // Helper functions for color manipulation
    function lighten(color, percent) {{
        const num = parseInt(color.replace("#", ""), 16);
        const amt = Math.round(2.55 * percent);
        const R = (num >> 16) + amt;
        const G = (num >> 8 & 0x00FF) + amt;
        const B = (num & 0x0000FF) + amt;
        return "#" + (0x1000000 + (R < 255 ? R < 1 ? 0 : R : 255) * 0x10000 +
            (G < 255 ? G < 1 ? 0 : G : 255) * 0x100 +
            (B < 255 ? B < 1 ? 0 : B : 255)).toString(16).slice(1);
    }}

    function darken(color, percent) {{
        const num = parseInt(color.replace("#", ""), 16);
        const amt = Math.round(2.55 * percent);
        const R = (num >> 16) - amt;
        const G = (num >> 8 & 0x00FF) - amt;
        const B = (num & 0x0000FF) - amt;
        return "#" + (0x1000000 + (R > 255 ? 255 : R < 0 ? 0 : R) * 0x10000 +
            (G > 255 ? 255 : G < 0 ? 0 : G) * 0x100 +
            (B > 255 ? 255 : B < 0 ? 0 : B)).toString(16).slice(1);
    }}

    // FIXED: Perfect button centering on panel border
    function updateCollapseButtonPosition() {{
        const toggleBtn = document.getElementById('sidebar-toggle');
        const panel = document.getElementById('info-panel');
        const isMobile = window.innerWidth <= 768;
        
        // Wait for CSS transitions to complete
        setTimeout(() => {{
            if (isMobile) {{
                // Mobile: position relative to actual panel position
                const panelRect = panel.getBoundingClientRect();
                
                toggleBtn.style.left = '50%';
                toggleBtn.style.right = 'auto';
                toggleBtn.style.top = 'auto';
                toggleBtn.style.transform = 'translateX(-50%)';
                
                if (sidebarCollapsed) {{
                    // FIXED: Center circle on panel border (60px - 18px radius = 42px)
                    toggleBtn.style.bottom = '42px';
                }} else {{
                    // Panel expanded, button at top edge of panel  
                    toggleBtn.style.bottom = 'calc(40% - 18px)';
                }}
            }} else {{
                // Desktop: position relative to side panel
                toggleBtn.style.top = '50%';
                toggleBtn.style.bottom = 'auto';
                toggleBtn.style.left = 'auto';
                toggleBtn.style.transform = 'translateY(-50%)';
                
                if (sidebarCollapsed) {{
                    // Panel hidden, button at screen edge
                    toggleBtn.style.right = '20px';
                }} else {{
                    // Panel visible, button at panel edge
                    toggleBtn.style.right = '360px';
                }}
            }}
        }}, 50); // Small delay to ensure CSS has applied
    }}

    // Correct arrow directions
    function updateArrowDirection() {{
        const toggleBtn = document.getElementById('sidebar-toggle');
        const arrow = toggleBtn.querySelector('i');
        const isMobile = window.innerWidth <= 768;
        
        if (isMobile) {{
            // Mobile: up/down arrows
            if (sidebarCollapsed) {{
                arrow.className = 'fas fa-chevron-up'; // Panel hidden, point up to expand
            }} else {{
                arrow.className = 'fas fa-chevron-down'; // Panel visible, point down to collapse
            }}
        }} else {{
            // Desktop: left/right arrows
            if (sidebarCollapsed) {{
                arrow.className = 'fas fa-chevron-left'; // Panel hidden, point left to expand
            }} else {{
                arrow.className = 'fas fa-chevron-right'; // Panel visible, point right to collapse
            }}
        }}
    }}

    // Detect layout mode changes for proper repositioning
    function detectLayoutModeChange() {{
        const newLayoutMode = window.innerWidth <= 768 ? 'mobile' : 'desktop';
        
        if (currentLayoutMode !== null && currentLayoutMode !== newLayoutMode) {{
            // Layout mode changed, reset everything
            const panel = document.getElementById('info-panel');
            const container = document.getElementById('network-container');
            
            // Clear all state classes
            panel.classList.remove('collapsed', 'expanded');
            container.classList.remove('sidebar-collapsed', 'panel-expanded');
            
            // Set appropriate default state for new layout
            if (newLayoutMode === 'mobile') {{
                sidebarCollapsed = true; // Start collapsed on mobile
            }} else {{
                sidebarCollapsed = false; // Start expanded on desktop
            }}
            
            // Apply correct classes for new state
            if (newLayoutMode === 'mobile' && sidebarCollapsed) {{
                panel.classList.remove('expanded');
                container.classList.remove('panel-expanded');
            }} else if (newLayoutMode === 'desktop' && !sidebarCollapsed) {{
                panel.classList.remove('collapsed');
                container.classList.remove('sidebar-collapsed');
            }}
        }}
        
        currentLayoutMode = newLayoutMode;
    }}

    // Enhanced node styling with no text labels
    function getNodeStyling(node, isDark = false) {{
        const nodeType = node.type || node.nodeType;
        const colors = HARMONIZED_COLORS[nodeType] || HARMONIZED_COLORS['Chunk'];
        
        const borderColor = isDark ? lighten(colors.border, 20) : darken(colors.border, 10);
        
        const baseStyle = {{
            borderWidth: 2,
            color: {{
                background: colors.bg,
                border: borderColor,
                highlight: {{ background: colors.bg, border: borderColor }},
                hover: {{ background: colors.bg, border: borderColor }}
            }},
            shadow: {{
                enabled: true,
                color: 'rgba(0,0,0,0.1)',
                size: 8,
                x: 2,
                y: 2
            }},
            label: '', // No text on nodes
            chosen: {{
                node: function(values, id, selected, hovering) {{
                    values.shadowSize = selected ? 15 : 8;
                    // Halo matches node color
                    values.shadowColor = selected ? colors.bg + '60' : 'rgba(0,0,0,0.1)';
                    values.borderWidth = selected ? 3 : 2;
                }}
            }}
        }};
        
        // Shape-specific styling
        if (nodeType === 'Document') {{
            return {{ 
                ...baseStyle, 
                shape: 'box', 
                size: 30, 
                borderRadius: 8
            }};
        }} else if (nodeType === 'Chunk') {{
            return {{ 
                ...baseStyle, 
                shape: 'box', 
                size: 22, 
                borderRadius: 6
            }};
        }} else {{
            return {{ 
                ...baseStyle, 
                shape: 'dot', 
                size: 18
            }};
        }}
    }}

    // Enhanced edge styling - all solid, better visibility
    function getEdgeStyling(edge, isDark = false) {{
        const edgeColorMap = {{
            light: {{
                'CONTAINS': '#6366F1',
                'NEXT': '#20C997',
                'MENTIONS': '#3B82F6', 
                'REFERENCES': '#F59E0B',
                'PART_OF': '#8B5CF6',
                'DESCRIBES': '#10B981'
            }},
            dark: {{
                'CONTAINS': '#818CF8',
                'NEXT': '#4dd4ac',
                'MENTIONS': '#60A5FA', 
                'REFERENCES': '#FBBF24',
                'PART_OF': '#A78BFA',
                'DESCRIBES': '#34D399'
            }}
        }};
        
        const colorSet = isDark ? edgeColorMap.dark : edgeColorMap.light;
        const color = colorSet[edge.label] || (isDark ? '#8b949e' : '#57606a');
        
        return {{
            color: color,
            width: 2,
            dashes: false,
            arrows: {{ to: {{ enabled: true, scaleFactor: 1.2 }} }},
            font: {{ 
                align: 'top', 
                color: isDark ? '#8b949e' : '#57606a', 
                size: 12,
                background: isDark ? 'rgba(13, 17, 23, 0.9)' : 'rgba(255, 255, 255, 0.9)',
                strokeWidth: 2,
                strokeColor: isDark ? '#0d1117' : '#ffffff'
            }},
            smooth: {{ type: 'dynamic', roundness: 0.5 }}
        }};
    }}
    
    // Theme management
    function initializeTheme() {{
        const savedTheme = localStorage.getItem('graph-theme') || 'light';
        setTheme(savedTheme);
    }}
    
    function setTheme(theme) {{
        document.documentElement.setAttribute('data-theme', theme);
        localStorage.setItem('graph-theme', theme);
        
        const sunIcon = document.querySelector('.sun-icon');
        const moonIcon = document.querySelector('.moon-icon');
        
        if (theme === 'dark') {{
            sunIcon.style.display = 'none';
            moonIcon.style.display = 'block';
        }} else {{
            sunIcon.style.display = 'block';
            moonIcon.style.display = 'none';
        }}
        
        if (network) {{
            updateNetworkTheme(theme);
        }}
    }}
    
    function updateNetworkTheme(theme) {{
        const isDark = theme === 'dark';
        
        // Update all nodes with new theme
        const updatedNodes = nodes.map(node => {{
            const styling = getNodeStyling(node, isDark);
            return {{ ...node, ...styling }};
        }});
        nodes.update(updatedNodes);
        
        // Update all edges with new theme
        const updatedEdges = edges.map(edge => {{
            const styling = getEdgeStyling(edge, isDark);
            return {{ 
                ...edge, 
                color: styling.color,
                width: styling.width,
                dashes: styling.dashes,
                font: styling.font
            }};
        }});
        edges.update(updatedEdges);
    }}
    
    // Proper sidebar toggle with dynamic positioning
    window.toggleSidebar = function() {{
        const panel = document.getElementById('info-panel');
        const container = document.getElementById('network-container');
        const isMobile = window.innerWidth <= 768;
        
        sidebarCollapsed = !sidebarCollapsed;
        
        if (isMobile) {{
            if (sidebarCollapsed) {{
                panel.classList.remove('expanded');
                container.classList.remove('panel-expanded');
            }} else {{
                panel.classList.add('expanded');
                container.classList.add('panel-expanded');
            }}
        }} else {{
            if (sidebarCollapsed) {{
                panel.classList.add('collapsed');
                container.classList.add('sidebar-collapsed');
            }} else {{
                panel.classList.remove('collapsed');
                container.classList.remove('sidebar-collapsed');
            }}
        }}
        
        // Update button position and arrow direction
        updateCollapseButtonPosition();
        updateArrowDirection();
        
        // Resize network after animation
        setTimeout(() => {{
            if (network) {{
                network.redraw();
                network.fit();
            }}
        }}, 300);
    }};
    
    // Mobile support initialization with proper orientation change handling
    function initializeMobileSupport() {{
        // Set initial layout mode
        currentLayoutMode = window.innerWidth <= 768 ? 'mobile' : 'desktop';
        
        if (currentLayoutMode === 'mobile') {{
            sidebarCollapsed = true;
            document.getElementById('info-panel').classList.remove('expanded');
            document.getElementById('network-container').classList.remove('panel-expanded');
        }}
        
        // Set initial button position and arrow
        updateCollapseButtonPosition();
        updateArrowDirection();
        
        // Handle orientation changes with proper delay
        window.addEventListener('orientationchange', function() {{
            setTimeout(() => {{
                detectLayoutModeChange();
                updateCollapseButtonPosition();
                updateArrowDirection();
                if (network) {{
                    network.redraw();
                    network.fit();
                }}
            }}, 700);
        }});
        
        // Handle window resize with layout mode detection
        window.addEventListener('resize', function() {{
            detectLayoutModeChange();
            
            // Use longer delay for better positioning
            setTimeout(() => {{
                updateCollapseButtonPosition();
                updateArrowDirection();
            }}, 100);
            
            setTimeout(() => {{
                if (network) {{
                    network.redraw();
                }}
            }}, 200);
        }});
    }}
    
    // Enhanced node creation
    let nodes = new vis.DataSet(graphData.nodes.map(node => {{
        const styling = getNodeStyling(node);
        
        return {{
            id: node.id,
            ...styling,
            nodeType: node.type,
            details: node.details
        }};
    }}));
    
    // Enhanced edge creation
    let edges = new vis.DataSet(graphData.edges.map(edge => {{
        const styling = getEdgeStyling(edge);
        return {{
            id: `${{edge.from}}_${{edge.to}}_${{edge.label}}`,
            from: edge.from,
            to: edge.to,
            label: edge.label,
            ...styling,
            details: edge.details
        }};
    }}));

    // Helper functions
    function getNodeDisplayLabel(node) {{
        const props = node.details?.properties || {{}};
        
        if (node.type === 'Chunk') {{
            return props.chunk_type || 'Chunk';
        }}
        if (node.type === 'Document') {{
            return props.filename || 'Document';
        }}
        return props.name || node.id || 'Unknown';
    }}

    function getNodeLabel(node) {{
        if (!node) return 'Unknown Node';
        return getNodeDisplayLabel(node);
    }}

    function getNodePreview(node) {{
        if (!node) return 'Unknown Node';
        
        const props = node.details?.properties || {{}};
        
        if (node.nodeType === 'Chunk') {{
            const text = props.text || '';
            return text.length > 50 ? text.substring(0, 50) + '...' : text;
        }} else if (node.nodeType === 'Document') {{
            return `Document with ${{props.chunk_count || 'unknown'}} chunks`;
        }} else {{
            return props.name || node.id || 'Unknown';
        }}
    }}

    // Professional property formatting
    function formatPropertyValue(key, value) {{
        if (!value && value !== 0) return '<span style="color: var(--text-muted); font-style: italic;">Not specified</span>';
        
        // Email addresses
        if (typeof value === 'string' && value.includes('@') && value.includes('.')) {{
            return `<a href="mailto:${{value}}" class="data-link">${{value}}</a>`;
        }}
        
        // URLs
        if (typeof value === 'string' && value.startsWith('http')) {{
            const domain = new URL(value).hostname;
            return `<a href="${{value}}" target="_blank" class="data-link">${{domain}}</a>`;
        }}
        
        // Dates
        if (key.toLowerCase().includes('date') || key.toLowerCase().includes('time')) {{
            try {{
                const date = new Date(value);
                return date.toLocaleDateString('en-US', {{ 
                    year: 'numeric', 
                    month: 'short', 
                    day: 'numeric',
                    hour: value.includes('T') ? '2-digit' : undefined,
                    minute: value.includes('T') ? '2-digit' : undefined
                }});
            }} catch {{
                return value;
            }}
        }}
        
        // Currency
        if (key.toLowerCase().includes('amount') || key.toLowerCase().includes('price') || key.toLowerCase().includes('revenue')) {{
            const num = parseFloat(value);
            if (!isNaN(num)) {{
                return new Intl.NumberFormat('en-US', {{ 
                    style: 'currency', 
                    currency: 'USD',
                    minimumFractionDigits: 0,
                    maximumFractionDigits: 0
                }}).format(num);
            }}
        }}
        
        // Large numbers
        if (typeof value === 'number' && value > 1000) {{
            return new Intl.NumberFormat('en-US').format(value);
        }}
        
        // Arrays as tags
        if (Array.isArray(value)) {{
            return value.map(v => `<span class="tag">${{v}}</span>`).join(' ');
        }}
        
        // Long text with expand/collapse
        if (typeof value === 'string' && value.length > 150) {{
            const preview = value.substring(0, 150);
            return `<details style="cursor: pointer;">
                <summary style="color: var(--accent-primary); font-size: var(--font-size-sm);">
                    ${{preview}}... <em>(click to expand)</em>
                </summary>
                <div style="margin-top: var(--space-sm); font-family: monospace; font-size: var(--font-size-xs); line-height: var(--line-height-relaxed);">${{value}}</div>
            </details>`;
        }}
        
        // Objects
        if (typeof value === 'object' && value !== null) {{
            return `<details style="cursor: pointer;">
                <summary style="color: var(--accent-primary);">View object data</summary>
                <pre style="margin-top: var(--space-sm); font-size: var(--font-size-xs); overflow-x: auto;">${{JSON.stringify(value, null, 2)}}</pre>
            </details>`;
        }}
        
        return value;
    }}

    const container = document.getElementById('network-container');
    const data = {{ nodes: nodes, edges: edges }};
    
    const options = {{
        physics: {{
            enabled: true,
            solver: 'barnesHut',
            barnesHut: {{
                gravitationalConstant: -8000,
                centralGravity: 0.3,
                springLength: 150,
                springConstant: 0.04,
                damping: 0.09,
                avoidOverlap: 0.1
            }},
            stabilization: {{
                enabled: true,
                iterations: 1000,
                updateInterval: 25
            }}
        }},
        interaction: {{
            hover: true,
            tooltipDelay: 300,
            hideEdgesOnDrag: false,
            hideNodesOnDrag: false
        }},
        nodes: {{
            borderWidthSelected: 3
        }},
        edges: {{
            hoverWidth: 0.5,
            selectionWidth: 3
        }}
    }};
    
    network = new vis.Network(container, data, options);

    // Enhanced tooltip event handlers
    network.on('hoverNode', function(params) {{
        const nodeId = params.node;
        const node = nodes.get(nodeId);
        const canvasPosition = network.canvasToDOM(params.pointer.canvas);
        showCustomTooltip(node, canvasPosition.x, canvasPosition.y, 'node');
    }});

    network.on('hoverEdge', function(params) {{
        const edgeId = params.edge;
        const edge = edges.get(edgeId);
        const canvasPosition = network.canvasToDOM(params.pointer.canvas);
        showCustomTooltip(edge, canvasPosition.x, canvasPosition.y, 'edge');
    }});

    network.on('blurNode', hideCustomTooltip);
    network.on('blurEdge', hideCustomTooltip);

    // Enhanced click handlers with proper selection tracking
    network.on('click', function(params) {{
        if (params.nodes.length > 0) {{
            const nodeId = params.nodes[0];
            const node = nodes.get(nodeId);
            showNodeDetails(node);
            document.getElementById('selected-count').textContent = '1';
            document.getElementById('selected-label').textContent = 'Node';
        }} else if (params.edges.length > 0) {{
            const edgeId = params.edges[0];
            const edge = edges.get(edgeId);
            showEdgeDetails(edge);
            document.getElementById('selected-count').textContent = '1';
            document.getElementById('selected-label').textContent = 'Edge';
        }} else {{
            showEmptyState();
            document.getElementById('selected-count').textContent = '0';
            document.getElementById('selected-label').textContent = 'Selected';
        }}
    }});

    // Enhanced tooltip generation
    function showCustomTooltip(item, x, y, type) {{
        let content = '';
        
        if (type === 'node') {{
            const nodeType = item.nodeType || 'Unknown';
            const typeClass = nodeType.toLowerCase();
            const props = item.details?.properties || {{}};
            
            if (nodeType === 'Chunk') {{
                const chunkType = props.chunk_type || 'Chunk';
                const text = props.text || '';
                const preview = text.length > 100 ? text.substring(0, 100) + '...' : text;
                
                content = `
                    <div class="tooltip-type ${{typeClass}}">${{nodeType}}</div>
                    <div class="tooltip-title">${{chunkType}}</div>`;
                
                if (props.speaker) {{
                    const speakerIcon = props.speaker === 'User' ? '👤' : '🤖';
                    content += `<div class="tooltip-meta">${{speakerIcon}} ${{props.speaker}}</div>`;
                }}
                
                if (props.order !== undefined) {{
                    content += `<div class="tooltip-meta"># ${{props.order}}</div>`;
                }}
                
                content += `<div class="tooltip-content">${{preview}}</div>`;
                
            }} else if (nodeType === 'Document') {{
                const filename = props.filename || 'Document';
                content = `
                    <div class="tooltip-type ${{typeClass}}">${{nodeType}}</div>
                    <div class="tooltip-title">${{filename}}</div>
                    <div class="tooltip-content">Root document node</div>
                `;
            }} else {{
                const name = props.name || 'Entity';
                content = `
                    <div class="tooltip-type ${{typeClass}}">${{nodeType}}</div>
                    <div class="tooltip-title">${{name}}</div>
                    <div class="tooltip-content">Click to view details</div>
                `;
            }}
        }} else if (type === 'edge') {{
            const sourceNode = nodes.get(item.from);
            const targetNode = nodes.get(item.to);
            const sourceLabel = getNodeLabel(sourceNode);
            const targetLabel = getNodeLabel(targetNode);
            
            content = `
                <div class="tooltip-type relationship">${{item.label || 'Connection'}}</div>
                <div class="tooltip-title">${{sourceLabel}} → ${{targetLabel}}</div>
                <div class="tooltip-content">Click for details</div>
            `;
        }}
        
        tooltip.innerHTML = content;
        
        // Position tooltip
        const rect = container.getBoundingClientRect();
        const tooltipRect = tooltip.getBoundingClientRect();
        
        let left = rect.left + x + 15;
        let top = rect.top + y - 10;
        
        if (left + tooltipRect.width > window.innerWidth) {{
            left = rect.left + x - tooltipRect.width - 15;
        }}
        if (top + tooltipRect.height > window.innerHeight) {{
            top = rect.top + y - tooltipRect.height - 10;
        }}
        
        tooltip.style.left = left + 'px';
        tooltip.style.top = top + 'px';
        tooltip.classList.add('visible');
    }}

    function hideCustomTooltip() {{
        tooltip.classList.remove('visible');
    }}

    // Enhanced node details display with professional icons
    function showNodeDetails(node) {{
        const title = document.getElementById('selection-title');
        const content = document.getElementById('selection-content');
        
        const nodeType = node.nodeType || 'Unknown';
        const icon = getNodeIcon(nodeType);
        title.innerHTML = `<i class="${{icon}}"></i> ${{nodeType}}`;
        
        const props = node.details?.properties || {{}};
        
        let propertyDisplay = '<div class="property-display">';
        
        // Core Information Section
        propertyDisplay += `
            <div class="property-section">
                <div class="property-section-header">Core Information</div>
                <div class="property-section-content">
                    <div class="property-grid">
        `;
        
        // Handle different node types
        if (nodeType === 'Chunk') {{
            if (props.chunk_type) {{
                propertyDisplay += `
                    <div class="property-item">
                        <span class="property-label">Type</span>
                        <span class="property-value">${{props.chunk_type}}</span>
                    </div>
                `;
            }}
            if (props.speaker) {{
                const speakerIcon = props.speaker === 'User' ? '👤' : '🤖';
                propertyDisplay += `
                    <div class="property-item">
                        <span class="property-label">Speaker</span>
                        <span class="property-value speaker" data-speaker-icon="${{speakerIcon}}">${{props.speaker}}</span>
                    </div>
                `;
            }}
            if (props.order !== undefined) {{
                propertyDisplay += `
                    <div class="property-item">
                        <span class="property-label">Order</span>
                        <span class="property-value">#${{props.order}}</span>
                    </div>
                `;
            }}
        }} else if (nodeType === 'Document') {{
            if (props.filename) {{
                propertyDisplay += `
                    <div class="property-item">
                        <span class="property-label">Filename</span>
                        <span class="property-value">${{props.filename}}</span>
                    </div>
                `;
            }}
            if (props.chunk_count) {{
                propertyDisplay += `
                    <div class="property-item">
                        <span class="property-label">Chunks</span>
                        <span class="property-value">${{props.chunk_count}}</span>
                    </div>
                `;
            }}
        }} else {{
            // Entity nodes
            if (props.name) {{
                propertyDisplay += `
                    <div class="property-item">
                        <span class="property-label">Name</span>
                        <span class="property-value">${{props.name}}</span>
                    </div>
                `;
            }}
        }}
        
        propertyDisplay += '</div></div></div>';
        
        // Text Content Section (for chunks)
        if (nodeType === 'Chunk' && props.text) {{
            propertyDisplay += `
                <div class="property-section">
                    <div class="property-section-header">Content</div>
                    <div class="property-section-content">
                        <div class="property-value text-content">${{props.text}}</div>
                    </div>
                </div>
            `;
        }}
        
        // Metadata Section (other properties)
        const coreProps = new Set(['chunk_type', 'speaker', 'order', 'text', 'name', 'filename', 'chunk_count']);
        const otherProps = Object.entries(props).filter(([key]) => !coreProps.has(key));
        
        if (otherProps.length > 0) {{
            propertyDisplay += `
                <div class="property-section">
                    <div class="property-section-header">Metadata</div>
                    <div class="property-section-content">
                        <div class="property-grid">
            `;
            
            otherProps.forEach(([key, value]) => {{
                const formattedValue = formatPropertyValue(key, value);
                
                propertyDisplay += `
                    <div class="property-item">
                        <span class="property-label">${{key.replace(/_/g, ' ')}}</span>
                        <span class="property-value">${{formattedValue}}</span>
                    </div>
                `;
            }});
            
            propertyDisplay += '</div></div></div>';
        }}
        
        propertyDisplay += '</div>';
        content.innerHTML = propertyDisplay;
    }}

    // Enhanced edge details display
    function showEdgeDetails(edge) {{
        const title = document.getElementById('selection-title');
        const content = document.getElementById('selection-content');
        
        const sourceNode = nodes.get(edge.from);
        const targetNode = nodes.get(edge.to);
        
        title.innerHTML = '<i class="fas fa-link"></i> Relationship';
        
        content.innerHTML = `
            <div class="property-display">
                <div class="relationship-summary">
                    <div class="relationship-vertical">
                        <div class="relationship-node-full">${{getNodeLabel(sourceNode)}}</div>
                        <div class="relationship-arrow-vertical">
                            <i class="fas fa-arrow-down" style="font-size: 16px;"></i>
                            <div class="relationship-label">${{edge.label || 'Connection'}}</div>
                        </div>
                        <div class="relationship-node-full">${{getNodeLabel(targetNode)}}</div>
                    </div>
                </div>
            </div>
        `;
    }}

    // Mobile-aware empty state
    function showEmptyState() {{
        const title = document.getElementById('selection-title');
        const content = document.getElementById('selection-content');
        
        title.innerHTML = '<i class="fas fa-mouse-pointer"></i> Selection Details';
        
        const isMobile = window.innerWidth <= 768;
        const instruction = isMobile ? 'Tap any node or edge' : 'Click any node or edge';
        
        content.innerHTML = `
            <div class="empty-state">
                <i class="fas fa-project-diagram" style="font-size: 48px; color: var(--text-muted); margin-bottom: var(--space-md);"></i>
                <h4 style="margin: 0 0 var(--space-sm) 0; color: var(--text-primary); font-size: var(--font-size-md);">Explore the Graph</h4>
                <p style="margin: 0; font-size: var(--font-size-sm); color: var(--text-secondary);">${{instruction}} to view detailed information and relationships.</p>
            </div>
        `;
    }}

    function updateStatistics() {{
        const totalNodes = nodes.length;
        const totalEdges = edges.length;
        
        document.getElementById('total-nodes').textContent = totalNodes;
        document.getElementById('total-edges').textContent = totalEdges;
        document.getElementById('selected-count').textContent = '0';
        document.getElementById('selected-label').textContent = 'Selected';
    }}
    
    // Global control functions
    window.fitNetwork = function() {{
        network.fit({{ animation: {{ duration: 1000, easingFunction: 'easeInOutQuad' }} }});
    }};
    
    window.togglePhysics = function() {{
        physicsEnabled = !physicsEnabled;
        network.setOptions({{ physics: {{ enabled: physicsEnabled }} }});
        
        const btn = document.getElementById('physics-btn');
        if (physicsEnabled) {{
            btn.classList.add('active');
        }} else {{
            btn.classList.remove('active');
        }}
    }};

    window.toggleTheme = function() {{
        const currentTheme = document.documentElement.getAttribute('data-theme') || 'light';
        const newTheme = currentTheme === 'light' ? 'dark' : 'light';
        setTheme(newTheme);
    }};

    // Initialize everything
    initializeTheme();
    initializeMobileSupport();
    updateStatistics();
    showEmptyState();
    
    // Auto-fit after stabilization
    network.once('stabilizationIterationsDone', function() {{
        setTimeout(() => {{
            network.fit({{ animation: {{ duration: 1000 }} }});
        }}, 500);
    }});
}});
</script>

</body>
</html>
"""


def convert_neo4j_to_vis_json(graph_records: List[Dict]) -> Dict[str, Any]:
    """
    Converts the raw, aggregated Neo4j query result into Vis.js format.
    Enhanced to handle all node types properly.
    """
    nodes_map = {}
    edges_set = set()

    if not graph_records or not isinstance(graph_records, list) or not graph_records[0]:
        return {"nodes": [], "edges": []}

    record = graph_records[0]

    # Process nodes
    for node in record.get("nodes", []):
        if "id" not in node:
            continue

        props = node.get("props", {})
        node_label = node.get("label", "Unknown")

        # Enhanced node data structure
        nodes_map[node["id"]] = {
            "id": node["id"],
            "label": _get_display_label(node_label, props),
            "type": node_label,  # Keep original label for styling
            "details": {
                "id": node.get("id"),
                "label": node_label,
                "properties": props,
            },
        }

    # Process relationships
    for rel in record.get("rels", []):
        if "start_node_id" in rel and "end_node_id" in rel:
            edges_set.add(
                (
                    rel["start_node_id"],
                    rel["end_node_id"],
                    rel.get("type", "RELATED"),
                    json.dumps(rel),
                )
            )

    return {
        "nodes": list(nodes_map.values()),
        "edges": [
            {"from": s, "to": t, "label": l, "details": json.loads(d)}
            for s, t, l, d in edges_set  # noqa: E741
        ],
    }


def _get_display_label(node_label: str, props: Dict[str, Any]) -> str:
    """Helper to get appropriate display label for different node types."""
    if node_label == "Chunk":
        return props.get("chunk_type", "Chunk")
    elif node_label == "Document":
        return props.get("filename", "Document")
    else:
        # Entity nodes
        return props.get("name", node_label)


def generate_html_visualization(
    graph_records: List[Dict],
    output_path: Optional[str] = None,
    return_html: bool = False,
):
    """Generates a self-contained HTML visualization from raw Neo4j graph records."""
    vis_data = convert_neo4j_to_vis_json(graph_records)
    json_string = json.dumps(vis_data, indent=2, default=str)
    final_html = HTML_TEMPLATE.format(graph_data=json_string)

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(final_html)

    if return_html:
        return final_html
