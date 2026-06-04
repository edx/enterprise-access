#!/usr/bin/env python3
"""
Integration tests for Billing Management API.

This script runs integration tests against the billing management API
using real OAuth authentication and Stripe sandbox data.

Usage Examples:
    # Run all tests with default .env file
    python scripts/test_billing_integration.py --all

    # Run specific test
    python scripts/test_billing_integration.py --test get_address

    # Run multiple tests
    python scripts/test_billing_integration.py --test get_address --test update_address

    # Use custom .env file with verbose output
    python scripts/test_billing_integration.py --all --env .env.staging --verbose

    # List available tests
    python scripts/test_billing_integration.py --list

    # Run with debug logging
    python scripts/test_billing_integration.py --all --debug
"""
import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Set

# Add scripts directory to path to import billing_integration package
sys.path.insert(0, str(Path(__file__).parent))

from billing_integration.auth import JWTAuthenticator
from billing_integration.client import BillingManagementClient
from billing_integration.config import Config
from billing_integration.test_scenarios import TEST_SCENARIOS, get_scenario_by_name, list_scenario_names


# ANSI color codes for terminal output
class Colors:
    """ANSI color codes for colored terminal output."""
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def setup_logging(verbose: bool = False, debug: bool = False) -> None:
    """
    Configure logging for the test run.

    Args:
        verbose: Enable verbose logging (INFO level)
        debug: Enable debug logging (DEBUG level)
    """
    if debug:
        level = logging.DEBUG
    elif verbose:
        level = logging.INFO
    else:
        level = logging.WARNING

    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S'
    )


def print_header(text: str) -> None:
    """Print a colored header."""
    print(f"\n{Colors.BOLD}{Colors.HEADER}{text}{Colors.ENDC}")
    print("=" * len(text))


def print_success(text: str) -> None:
    """Print success message in green."""
    print(f"{Colors.GREEN}✓ {text}{Colors.ENDC}")


def print_error(text: str) -> None:
    """Print error message in red."""
    print(f"{Colors.RED}✗ {text}{Colors.ENDC}")


def print_warning(text: str) -> None:
    """Print warning message in yellow."""
    print(f"{Colors.YELLOW}⚠ {text}{Colors.ENDC}")


def print_info(text: str) -> None:
    """Print info message in cyan."""
    print(f"{Colors.CYAN}ℹ {text}{Colors.ENDC}")


def resolve_dependencies(test_names: List[str]) -> List[str]:
    """
    Resolve test dependencies and return ordered list.

    Args:
        test_names: List of test names to run

    Returns:
        Ordered list of test names with dependencies resolved

    Raises:
        ValueError: If circular dependency detected or dependency not found
    """
    resolved = []
    seen = set()

    def resolve(name: str, path: Set[str]) -> None:
        if name in path:
            raise ValueError(f"Circular dependency detected: {' -> '.join(path)} -> {name}")

        if name in seen:
            return

        scenario = get_scenario_by_name(name)
        if not scenario:
            raise ValueError(f"Test scenario not found: {name}")

        if scenario.depends_on:
            for dep in scenario.depends_on:
                resolve(dep, path | {name})

        if name not in seen:
            resolved.append(name)
            seen.add(name)

    for test_name in test_names:
        resolve(test_name, set())

    return resolved


def run_test(
    scenario,
    client: BillingManagementClient,
    config: Config,
    verbose: bool = False
) -> Dict:
    """
    Run a single test scenario.

    Args:
        scenario: TestScenario to run
        client: Billing management client
        config: Configuration
        verbose: Whether to show verbose output

    Returns:
        Dictionary with test results
    """
    print(f"\n{Colors.BOLD}▶ Running: {scenario.name}{Colors.ENDC}")
    print(f"  {scenario.description}")

    try:
        result = scenario.run(client, config)

        if result['status'] == 'PASS':
            print_success(f"{scenario.name}: PASSED")
        else:
            print_error(f"{scenario.name}: {result['status']}")

        if verbose and 'data' in result:
            print(f"\n{Colors.CYAN}  Response data:{Colors.ENDC}")
            print(f"  {json.dumps(result['data'], indent=2)}")

        return result

    except AssertionError as e:
        print_error(f"{scenario.name}: ASSERTION FAILED")
        print(f"  {e}")
        return {'status': 'FAIL', 'error': str(e), 'error_type': 'assertion'}

    except Exception as e:
        print_error(f"{scenario.name}: ERROR")
        print(f"  {type(e).__name__}: {e}")
        return {'status': 'FAIL', 'error': str(e), 'error_type': type(e).__name__}


def main() -> int:
    """Main entry point for the test script."""
    parser = argparse.ArgumentParser(
        description='Billing Management API Integration Tests',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument(
        '--env',
        type=Path,
        default=Path('.env'),
        help='Path to .env file (default: .env)'
    )
    parser.add_argument(
        '--test',
        action='append',
        dest='tests',
        help='Run specific test by name (can be specified multiple times)'
    )
    parser.add_argument(
        '--all',
        action='store_true',
        help='Run all tests'
    )
    parser.add_argument(
        '--list',
        action='store_true',
        help='List available tests and exit'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Verbose output (show test data)'
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug logging'
    )
    parser.add_argument(
        '--stop-on-fail',
        action='store_true',
        help='Stop running tests after first failure'
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.verbose, args.debug)

    # List tests if requested
    if args.list:
        print_header("Available Test Scenarios")
        for scenario in TEST_SCENARIOS:
            deps = f" (depends on: {', '.join(scenario.depends_on)})" if scenario.depends_on else ""
            print(f"  • {Colors.BOLD}{scenario.name}{Colors.ENDC}{deps}")
            print(f"    {scenario.description}")
        return 0

    # Determine which tests to run
    if args.tests:
        test_names = args.tests
        # Validate test names
        invalid = [name for name in test_names if not get_scenario_by_name(name)]
        if invalid:
            print_error(f"Unknown test(s): {', '.join(invalid)}")
            print_info("Use --list to see available tests")
            return 1
    elif args.all:
        test_names = list_scenario_names()
    else:
        parser.print_help()
        return 1

    # Resolve dependencies
    try:
        test_names = resolve_dependencies(test_names)
    except ValueError as e:
        print_error(f"Dependency error: {e}")
        return 1

    # Load configuration
    print_header("Configuration")
    try:
        config = Config.from_env(args.env)
        config.validate()

        print_success("Configuration loaded successfully")
        if args.verbose:
            print("\nConfiguration (secrets masked):")
            for key, value in config.mask_secrets().items():
                print(f"  {key}: {value}")

    except FileNotFoundError as e:
        print_error(f"Environment file not found: {e}")
        print_info("Create a .env file or specify --env path")
        print_info("See .env.example for required variables")
        return 1
    except ValueError as e:
        print_error(f"Configuration error: {e}")
        return 1
    except Exception as e:
        print_error(f"Unexpected error loading configuration: {e}")
        return 1

    # Setup authentication and client
    print_header("Authentication")
    try:
        auth = JWTAuthenticator(
            config.oauth_client_id,
            config.oauth_client_secret,
            config.oauth_token_url
        )

        # Test authentication by getting a token
        token = auth.get_token()
        print_success(f"JWT token acquired (expires in {auth.token_expires_in}s)")

        if args.debug:
            print(f"\n  Token preview: {token[:20]}...{token[-20:]}")

    except Exception as e:
        print_error(f"Authentication failed: {e}")
        print_info("Check your OAuth credentials in .env file")
        return 1

    # Create API client
    client = BillingManagementClient(config.api_base_url, auth)
    print_info(f"API client configured for {config.api_base_url}")

    # Run tests
    print_header("Running Tests")
    print(f"Will run {len(test_names)} test(s)")

    results = {}
    skipped = []

    for test_name in test_names:
        scenario = get_scenario_by_name(test_name)

        # Check if dependencies passed
        if scenario.depends_on:
            failed_deps = [
                dep for dep in scenario.depends_on
                if results.get(dep, {}).get('status') != 'PASS'
            ]
            if failed_deps:
                print_warning(
                    f"Skipping {test_name} due to failed dependencies: {', '.join(failed_deps)}"
                )
                skipped.append(test_name)
                continue

        result = run_test(scenario, client, config, args.verbose)
        results[test_name] = result

        # Stop on failure if requested
        if args.stop_on_fail and result['status'] != 'PASS':
            print_warning("Stopping due to test failure (--stop-on-fail)")
            break

    # Print summary
    print_header("Test Summary")

    passed = sum(1 for r in results.values() if r['status'] == 'PASS')
    failed = sum(1 for r in results.values() if r['status'] != 'PASS')
    total = len(results)

    print(f"\nResults: {passed}/{total} passed")

    if failed > 0:
        print(f"\n{Colors.RED}Failed tests:{Colors.ENDC}")
        for name, result in results.items():
            if result['status'] != 'PASS':
                error_type = result.get('error_type', 'unknown')
                error_msg = result.get('error', 'No error message')
                print(f"  • {name}: {error_type}")
                if args.verbose:
                    print(f"    {error_msg}")

    if skipped:
        print(f"\n{Colors.YELLOW}Skipped tests:{Colors.ENDC}")
        for name in skipped:
            print(f"  • {name}")

    # Final status
    if passed == total and not skipped:
        print_success(f"\n🎉 All tests passed!")
        return 0
    elif failed > 0:
        print_error(f"\n❌ {failed} test(s) failed")
        return 1
    else:
        print_warning(f"\n⚠️  Some tests were skipped")
        return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print_warning("\n\nTest run interrupted by user")
        sys.exit(130)
    except Exception as e:
        print_error(f"\nUnexpected error: {e}")
        if '--debug' in sys.argv:
            raise
        sys.exit(1)
