#!/usr/bin/env python3
"""
Generate a cost optimization report combining idle EC2 and unused EBS findings.

Supports output formats: table (stdout), json (file).
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional

import boto3
from tabulate import tabulate
from botocore.exceptions import ClientError

# Import the other scripts' functions
sys.path.insert(0, os.path.dirname(__file__))
from idle_ec2 import find_idle_instances, get_all_regions as get_regions_ec2
from unused_ebs import find_unused_volumes, find_orphaned_snapshots

logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def get_aws_account_id(role_arn: Optional[str] = None) -> str:
    """Get the AWS account ID."""
    try:
        sts = boto3.client('sts')
        if role_arn:
            response = sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName='cost-optimizer'
            )
            credentials = response['Credentials']
            sts_client = boto3.client(
                'sts',
                aws_access_key_id=credentials['AccessKeyId'],
                aws_secret_access_key=credentials['SecretAccessKey'],
                aws_session_token=credentials['SessionToken']
            )
            identity = sts_client.get_caller_identity()
        else:
            identity = sts.get_caller_identity()
        return identity['Account']
    except ClientError as e:
        logger.error(f"Failed to get account ID: {e}")
        return "unknown"


def generate_report(regions: List[str], role_arn: Optional[str] = None) -> Dict:
    """Generate comprehensive cost report."""
    report = {
        'timestamp': datetime.utcnow().isoformat(),
        'account_id': get_aws_account_id(role_arn),
        'regions': regions,
        'findings': {
            'idle_ec2_instances': [],
            'unused_ebs_volumes': [],
            'orphaned_snapshots': []
        },
        'summary': {}
    }

    print("Generating cost optimization report...")

    # Scan each region
    for region in regions:
        print(f"  Scanning {region}...", end='', flush=True)
        try:
            idle_ec2 = find_idle_instances(region, cpu_threshold=5.0, role_arn=role_arn)
            report['findings']['idle_ec2_instances'].extend(idle_ec2)

            unused_volumes = find_unused_volumes(region, role_arn)
            report['findings']['unused_ebs_volumes'].extend(unused_volumes)

            orphaned_snaps = find_orphaned_snapshots(region, role_arn)
            report['findings']['orphaned_snapshots'].extend(orphaned_snaps)

            print(" ✓")
        except Exception as e:
            print(f" ✗ ({e})")

    # Calculate totals
    idle_ec2_cost = sum(f['estimated_monthly_cost'] for f in report['findings']['idle_ec2_instances'])
    unused_vol_cost = sum(f['estimated_monthly_cost'] for f in report['findings']['unused_ebs_volumes'])
    orphaned_snap_cost = sum(f['estimated_monthly_cost'] for f in report['findings']['orphaned_snapshots'])
    total_cost = idle_ec2_cost + unused_vol_cost + orphaned_snap_cost

    report['summary'] = {
        'idle_ec2_instances_count': len(report['findings']['idle_ec2_instances']),
        'idle_ec2_monthly_cost': round(idle_ec2_cost, 2),
        'unused_ebs_volumes_count': len(report['findings']['unused_ebs_volumes']),
        'unused_ebs_monthly_cost': round(unused_vol_cost, 2),
        'orphaned_snapshots_count': len(report['findings']['orphaned_snapshots']),
        'orphaned_snapshots_monthly_cost': round(orphaned_snap_cost, 2),
        'total_monthly_waste': round(total_cost, 2)
    }

    return report


def print_table_report(report: Dict) -> None:
    """Print report as formatted table to stdout."""
    summary = report['summary']
    findings = report['findings']

    print("\n" + "="*80)
    print("AWS COST OPTIMIZATION REPORT")
    print("="*80)
    print(f"Account: {report['account_id']}")
    print(f"Generated: {report['timestamp']}")
    print(f"Regions scanned: {', '.join(report['regions'])}")
    print()

    # Summary
    print("SUMMARY")
    print("-" * 80)
    summary_data = [
        ['Idle EC2 Instances', summary['idle_ec2_instances_count'], f"${summary['idle_ec2_monthly_cost']:.2f}"],
        ['Unused EBS Volumes', summary['unused_ebs_volumes_count'], f"${summary['unused_ebs_monthly_cost']:.2f}"],
        ['Orphaned Snapshots', summary['orphaned_snapshots_count'], f"${summary['orphaned_snapshots_monthly_cost']:.2f}"],
    ]
    print(tabulate(summary_data, headers=['Finding Type', 'Count', 'Monthly Cost'], tablefmt='simple'))
    print()
    print(f"TOTAL ESTIMATED MONTHLY WASTE: ${summary['total_monthly_waste']:.2f}")
    print(f"ANNUALIZED: ${summary['total_monthly_waste'] * 12:.2f}")
    print()

    # Idle instances
    if findings['idle_ec2_instances']:
        print("IDLE EC2 INSTANCES (avg CPU < 5% over 14 days)")
        print("-" * 80)
        idle_data = []
        for inst in sorted(findings['idle_ec2_instances'], key=lambda x: x['estimated_monthly_cost'], reverse=True):
            idle_data.append([
                inst['instance_id'],
                inst['instance_type'],
                f"{inst['avg_cpu']:.1f}%",
                inst['uptime_days'],
                f"${inst['estimated_monthly_cost']:.2f}",
                inst['region']
            ])
        print(tabulate(idle_data, headers=['Instance ID', 'Type', 'Avg CPU', 'Uptime (days)', 'Monthly Cost', 'Region'], tablefmt='simple'))
        print()

    # Unused volumes
    if findings['unused_ebs_volumes']:
        print("UNUSED EBS VOLUMES")
        print("-" * 80)
        vol_data = []
        for vol in sorted(findings['unused_ebs_volumes'], key=lambda x: x['estimated_monthly_cost'], reverse=True):
            vol_data.append([
                vol['volume_id'],
                f"{vol['size_gb']} GB",
                vol['type'],
                vol['age_days'],
                f"${vol['estimated_monthly_cost']:.2f}",
                vol['region']
            ])
        print(tabulate(vol_data, headers=['Volume ID', 'Size', 'Type', 'Age (days)', 'Monthly Cost', 'Region'], tablefmt='simple'))
        print()

    # Orphaned snapshots
    if findings['orphaned_snapshots']:
        print("ORPHANED SNAPSHOTS (> 90 days, no associated volume)")
        print("-" * 80)
        snap_data = []
        for snap in sorted(findings['orphaned_snapshots'], key=lambda x: x['estimated_monthly_cost'], reverse=True):
            snap_data.append([
                snap['snapshot_id'],
                f"{snap['size_gb']} GB",
                snap['age_days'],
                f"${snap['estimated_monthly_cost']:.2f}",
                snap['region']
            ])
        print(tabulate(snap_data, headers=['Snapshot ID', 'Size', 'Age (days)', 'Monthly Cost', 'Region'], tablefmt='simple'))
        print()

    print("="*80)


def save_json_report(report: Dict, filename: str) -> None:
    """Save report as JSON file."""
    with open(filename, 'w') as f:
        json.dump(report, f, indent=2)
    logger.info(f"Report saved to {filename}")


def main():
    parser = argparse.ArgumentParser(description='Generate cost optimization report')
    parser.add_argument('--region', help='Specific AWS region (default: all regions)')
    parser.add_argument('--role-arn', help='IAM role ARN for cross-account access')
    parser.add_argument('--output-format', choices=['table', 'json'], default='table',
                       help='Output format (default: table)')
    parser.add_argument('--output-file', help='Output file for JSON format')
    args = parser.parse_args()

    role_arn = args.role_arn or os.environ.get('AWS_ROLE_ARN')

    if args.region:
        regions = [args.region]
    else:
        ec2 = boto3.client('ec2')
        regions = get_regions_ec2(ec2)

    # Generate report
    report = generate_report(regions, role_arn)

    # Output
    if args.output_format == 'json':
        output_file = args.output_file or f"cost-report-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
        save_json_report(report, output_file)
        print(f"\nReport saved to {output_file}")
    else:
        print_table_report(report)


if __name__ == '__main__':
    main()
