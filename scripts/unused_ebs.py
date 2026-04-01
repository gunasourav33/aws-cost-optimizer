#!/usr/bin/env python3
"""
Find unused EBS volumes and orphaned snapshots.

Identifies:
- Volumes in 'available' state (not attached to any instance)
- Snapshots older than 90 days with no associated volume
"""

import argparse
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Approximate monthly costs for EBS volumes
EBS_PRICING = {
    'gp3': 0.10,  # per GB-month
    'gp2': 0.10,
    'io1': 0.125,  # base, plus provisioned IOPS
    'io2': 0.125,
    'st1': 0.045,  # throughput optimized
    'sc1': 0.025,  # cold
    'standard': 0.05,  # magnetic
}


def get_ec2_client(region: str, role_arn: Optional[str] = None):
    """Create EC2 client, optionally assuming a role."""
    if role_arn:
        sts = boto3.client('sts', region_name=region)
        try:
            assumed = sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName='cost-optimizer'
            )
            credentials = assumed['Credentials']
            return boto3.client(
                'ec2',
                region_name=region,
                aws_access_key_id=credentials['AccessKeyId'],
                aws_secret_access_key=credentials['SecretAccessKey'],
                aws_session_token=credentials['SessionToken']
            )
        except ClientError as e:
            logger.error(f"Failed to assume role {role_arn}: {e}")
            raise
    else:
        return boto3.client('ec2', region_name=region)


def find_unused_volumes(region: str, role_arn: Optional[str] = None) -> List[Dict]:
    """Find unattached EBS volumes in a region."""
    unused_volumes = []

    try:
        ec2 = get_ec2_client(region, role_arn)

        logger.info(f"Scanning for unused volumes in {region}...")

        paginator = ec2.get_paginator('describe_volumes')
        for page in paginator.paginate(Filters=[{'Name': 'status', 'Values': ['available']}]):
            for volume in page['Volumes']:
                volume_id = volume['VolumeId']
                size_gb = volume['Size']
                volume_type = volume['VolumeType']
                create_time = volume['CreateTime']

                # Calculate age
                age_days = (datetime.utcnow() - create_time.replace(tzinfo=None)).days

                # Estimate monthly cost
                monthly_cost = size_gb * EBS_PRICING.get(volume_type, 0.10)

                # Add provisioned IOPS cost if applicable
                if volume_type == 'io1' and 'Iops' in volume:
                    # io1 is $0.065 per provisioned IOPS-month
                    monthly_cost += volume['Iops'] * 0.065

                unused_volumes.append({
                    'region': region,
                    'volume_id': volume_id,
                    'size_gb': size_gb,
                    'type': volume_type,
                    'age_days': age_days,
                    'state': volume['State'],
                    'estimated_monthly_cost': round(monthly_cost, 2),
                    'create_time': create_time.isoformat()
                })
                logger.debug(f"Found unused volume: {volume_id} ({size_gb}GB {volume_type})")

    except ClientError as e:
        logger.error(f"Error scanning volumes in {region}: {e}")

    return unused_volumes


def find_orphaned_snapshots(region: str, role_arn: Optional[str] = None) -> List[Dict]:
    """Find snapshots older than 90 days with no associated volume."""
    orphaned_snapshots = []

    try:
        ec2 = get_ec2_client(region, role_arn)

        logger.info(f"Scanning for orphaned snapshots in {region}...")

        # Get all snapshots owned by account
        paginator = ec2.get_paginator('describe_snapshots')
        for page in paginator.paginate(OwnerIds=['self']):
            for snapshot in page['Snapshots']:
                snapshot_id = snapshot['SnapshotId']
                volume_id = snapshot.get('VolumeId')
                start_time = snapshot['StartTime']
                size_gb = snapshot['VolumeSize']

                # Calculate age
                age_days = (datetime.utcnow() - start_time.replace(tzinfo=None)).days

                # Consider orphaned if older than 90 days AND no associated volume
                if age_days > 90 and not volume_id:
                    # Snapshots cost about $0.05 per GB-month
                    monthly_cost = size_gb * 0.05

                    orphaned_snapshots.append({
                        'region': region,
                        'snapshot_id': snapshot_id,
                        'volume_id': volume_id,
                        'size_gb': size_gb,
                        'age_days': age_days,
                        'estimated_monthly_cost': round(monthly_cost, 2),
                        'start_time': start_time.isoformat()
                    })
                    logger.debug(f"Found orphaned snapshot: {snapshot_id} ({age_days} days old)")

    except ClientError as e:
        logger.error(f"Error scanning snapshots in {region}: {e}")

    return orphaned_snapshots


def get_all_regions(ec2_client) -> List[str]:
    """Get all available AWS regions."""
    try:
        response = ec2_client.describe_regions(AllRegions=False)
        return [region['RegionName'] for region in response['Regions']]
    except ClientError as e:
        logger.error(f"Failed to get regions: {e}")
        return []


def main():
    parser = argparse.ArgumentParser(description='Find unused EBS volumes and snapshots')
    parser.add_argument('--region', help='Specific AWS region (default: all regions)')
    parser.add_argument('--role-arn', help='IAM role ARN for cross-account access')
    args = parser.parse_args()

    role_arn = args.role_arn or os.environ.get('AWS_ROLE_ARN')

    regions = [args.region] if args.region else get_all_regions(boto3.client('ec2'))

    all_volumes = []
    all_snapshots = []

    for region in regions:
        volumes = find_unused_volumes(region, role_arn)
        all_volumes.extend(volumes)

        snapshots = find_orphaned_snapshots(region, role_arn)
        all_snapshots.extend(snapshots)

    if all_volumes:
        print(f"\nFound {len(all_volumes)} unused volumes:\n")
        for vol in sorted(all_volumes, key=lambda x: x['estimated_monthly_cost'], reverse=True):
            print(f"{vol['volume_id']:20} {vol['size_gb']:5}GB  {vol['type']:6}  "
                  f"Age: {vol['age_days']:4} days  ${vol['estimated_monthly_cost']:7.2f}/mo  ({vol['region']})")
    else:
        print("\nNo unused volumes found")

    if all_snapshots:
        print(f"\nFound {len(all_snapshots)} orphaned snapshots:\n")
        for snap in sorted(all_snapshots, key=lambda x: x['estimated_monthly_cost'], reverse=True):
            print(f"{snap['snapshot_id']:30} {snap['size_gb']:5}GB  "
                  f"Age: {snap['age_days']:4} days  ${snap['estimated_monthly_cost']:7.2f}/mo  ({snap['region']})")
    else:
        print("\nNo orphaned snapshots found")

    total_cost = sum(v['estimated_monthly_cost'] for v in all_volumes) + \
                 sum(s['estimated_monthly_cost'] for s in all_snapshots)

    if total_cost > 0:
        print(f"\nTotal estimated monthly waste: ${total_cost:.2f}")


if __name__ == '__main__':
    main()
