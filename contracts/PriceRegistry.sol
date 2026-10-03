// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/// @title PriceRegistry — StockGap oracle snapshot anchor (BSC mainnet, chain 56)
/// @notice Single owner (the deployer) anchors quantized oracle prices.
///         Each snapshotId may be anchored exactly once; timestamp comes from
///         the chain (block.timestamp), never from the caller.
contract PriceRegistry {
    address public owner;
    mapping(bytes32 => bool) public published;

    event OraclePublish(bytes32 indexed snapshotId, uint256 priceE8, uint256 timestamp);

    constructor() {
        owner = msg.sender;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "PriceRegistry: not owner");
        _;
    }

    /// @notice Anchor one oracle snapshot price (priceE8 = price x 1e8).
    function publish(bytes32 snapshotId, uint256 priceE8) external onlyOwner {
        require(!published[snapshotId], "PriceRegistry: duplicate snapshotId");
        published[snapshotId] = true;
        emit OraclePublish(snapshotId, priceE8, block.timestamp);
    }
}
