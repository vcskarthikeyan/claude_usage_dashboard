#!/bin/bash

echo "Stopping all existing dashboard processes..."
pkill -f "streamlit run app.py"
pkill -f "collector.py --daemon"
pkill -f "gemini_collector.py --daemon"

echo "Waiting for processes to terminate..."
sleep 3

echo "Starting Streamlit dashboard in background..."
# Ensure CLAUDE_ADMIN_API_KEY is set in the environment before running
# Example: export CLAUDE_ADMIN_API_KEY="your_api_key_here"
nohup .venv/bin/streamlit run app.py &

echo "Dashboard restarted. Check http://localhost:8501"