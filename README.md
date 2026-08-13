# ASX IBKR Execution Engine

Paper-trading execution layer for the ASX Opportunity Engine.

## Purpose

This repository is intentionally separate from the ASX Opportunity Engine. The Opportunity Engine remains the signal generator and dashboard; this project consumes ACTIVE trade plans and prepares/sends orders to Interactive Brokers.

## Initial safety posture

- Paper trading only
- 0.5% account risk per trade by default
- Maximum 4% total open portfolio risk
- Maximum 6 open positions
- Duplicate-position and duplicate-order protection
- Maximum entry-price drift guard
- Kill switch
- Dry-run mode enabled by default
- Complete execution log

## Planned flow

ASX Opportunity Engine -> ACTIVE signal -> validation -> position sizing -> IBKR bracket order -> execution log

A bracket contains:

1. Parent BUY order
2. Profit-taking SELL LIMIT
3. Protective SELL STOP

The two exit orders are linked to the parent order.

## Setup status

Version 1 scaffolding is being built now. Do not use this repository for live-money trading until paper testing is complete and `paper_only` safeguards have been deliberately reviewed.
