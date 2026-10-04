"""
shift_click_handler.py - Backward compatibility wrapper for md_click_handler
"""

try:
    from .md_click_handler import main
except ImportError:
    from md_click_handler import main

if __name__ == "__main__":
    main()
