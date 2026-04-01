#!/usr/bin/env python3
"""
Find idle EC2 instances based on CloudWatch CPU metrics.

Identifies instances with average CPU utilization < 5% over the last 14 days.
Outputs instance details and estimated monthly costs.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Approximate on-demand hourly rates (as of 2023)
INSTANCE_HOURLY_RATES = {
    't3.nano': 0.0104,
    't3.micro': 0.0208,
    't3.small': 0.0416,
    't3.medium': 0.0832,
    't3.large': 0.1664,
    't3.xlarge': 0.3328,
    't3.2xlarge': 0.6656,
    'm5.large': 0.096,
    'm5.xlarge': 0.192,
    'm5.2xlarge': 0.384,
    'm5.4xlarge': 0.768,
    'm5.12xlarge': 2.304,
    't2.micro': 0.0116,
    't2.small': 0.0232,
    't2.medium': 0.0464,
    't2.large': 0.0928,
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


def get_cloudwatch_client(region: str, role_arn: Optional[str] = None):
    """Create CloudWatch client, optionally assuming a role."""
    if role_arn:
        sts = boto3.client('sts', region_name=region)
        try:
            assumed = sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName='cost-optimizer'
            )
            credentials = assumed['Credentials']
            return boto3.client(
                'cloudwatch',
                region_name=region,
                aws_access_key_id=credentials['AccessKeyId'],
                aws_secret_access_key=credentials['SecretAccessKey'],
                aws_session_token=credentials['SessionToken']
            )
        except ClientError as e:
            logger.error(f"Failed to assume role {role_arn}: {e}")
            raise
    else:
        return boto3.client('cloudwatch', region_name=region)


def get_average_cpu(cloudwatch, instance_id: str, days: int = 14) -> Optional[float]:
    """Get average CPU utilization for an instance over the past N days."""
    try:
        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=days)

        response = cloudwatch.get_metric_statistics(
            Namespace='AWS/EC2',
            MetricName='CPUUtilization',
            Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}],
            StartTime=start_time,
            EndTime=end_time,
            Period=3600,  # 1-hour periods
            Statistics=['Average']
        )

        if not response['Datapoints']:
            return None

        # Calculate average across all datapoints
        values = [dp['Average'] for dp in response['Datapoints']]
        return sum(values) / len(values) if values else None

    except ClientError as e:
        logger.warning(f"Could not get CPU metrics for {instance_id}: {e}")
        return None


def get_instance_uptime(launch_time: datetime) -> int:
    """Get uptime in days."""
    return (datetime.utcnow() - launch_time.replace(tzinfo=None)).days


def estimate_monthly_cost(instance_type: str, uptime_days: int) -> float:
    """Estimate monthly cost based on instance type and uptime."""
    hourly_rate = INSTANCE_HOURLY_RATES.get(instance_type, 0.0)
    hours_per_month = 730  # average hours per month
    return hourly_rate * hours_per_month


def find_idle_instances(region: str, cpu_threshold: float = 5.0,
                       role_arn: Optional[str] = None) -> List[Dict]:
    """Find idle instances in a region."""
    idle_instances = []

    try:
        ec2 = get_ec2_client(region, role_arn)
        cw = get_cloudwatch_client(region, role_arn)

        logger.info(f"Scanning region {region}...")

        # Get all running instances
        paginator = ec2.get_paginator('describe_instances')
        for page in paginator.paginate(Filters=[{'Name': 'instance-state-name', 'Values': ['running']}]):
            for reservation in page['Reservations']:
                for instance in reservation['Instances']:
                    instance_id = instance['InstanceId']
                    instance_type = instance['InstanceType']
                    launch_time = instance['LaunchTime']

                    # Only check instances that have been running for at least 7 days
                    uptime = get_instance_uptime(launch_time)
                    if uptime < 7:
                        continue

                    # Get CPU metrics
                    avg_cpu = get_average_cpu(cw, instance_id)
                    if avg_cpu is None:
                        logger.debug(f"No CPU data for {instance_id}")
                        continue

                    # Check if idle
                    if avg_cpu < cpu_threshold:
                        monthly_cost = estimate_monthly_cost(instance_type, uptime)
                        idle_instances.append({
                            'region': region,
                            'instance_id': instance_id,
                            'instance_type': instance_type,
                            'avg_cpu': round(avg_cpu, 2),
                            'uptime_days': uptime,
                            'estimated_monthly_cost': round(monthly_cost, 2),
                            'launch_time': launch_time.isoformat()
                        })
                        logger.debug(f"Found idle instance: {instance_id} ({instance_type}, {avg_cpu:.1f}% CPU)")

    except ClientError as e:
        logger.error(f"Error scanning region {region}: {e}")

    return idle_instances


def get_all_regions(ec2_client) -> List[str]:
    """Get all available AWS regions."""
    try:
        response = ec2_client.describe_regions(AllRegions=False)
        return [region['RegionName'] for region in response['Regions']]
    except ClientError as e:
        logger.error(f"Failed to get regions: {e}")
        return []


def main():
    parser = argparse.ArgumentParser(description='Find idle EC2 instances')
    parser.add_argument('--region', help='Specific AWS region (default: all regions)')
    parser.add_argument('--cpu-threshold', type=float, default=5.0,
                       help='CPU threshold for idle instances (default: 5%)')
    parser.add_argument('--role-arn', help='IAM role ARN for cross-account access')
    args = parser.parse_args()

    role_arn = args.role_arn or os.environ.get('AWS_ROLE_ARN')

    regions = [args.region] if args.region else get_all_regions(boto3.client('ec2'))

    all_idle = []
    for region in regions:
        idle = find_idle_instances(region, args.cpu_threshold, role_arn)
        all_idle.extend(idle)

    if all_idle:
        print(f"\nFound {len(all_idle)} idle instances:\n")
        for inst in sorted(all_idle, key=lambda x: x['estimated_monthly_cost'], reverse=True):
            print(f"{inst['instance_id']:20} {inst['instance_type']:15} "
                  f"{inst['avg_cpu']:6.1f}% CPU  {inst['uptime_days']:4} days  "
                  f"${inst['estimated_monthly_cost']:8.2f}/mo  ({inst['region']})")
        print(f"\nTotal estimated monthly cost: ${sum(i['estimated_monthly_cost'] for i in all_idle):.2f}")
    else:
        print("No idle instances found")
        sys.exit(0)


if __name__ == '__main__':
    main()
