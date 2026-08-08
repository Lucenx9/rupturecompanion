#pragma once

#include <cstdint>
#include <filesystem>
#include <optional>
#include <string>

namespace RuptureCompanion::Bridge
{
struct Response
{
    std::uint64_t sequence;
    std::string sessionId;
    bool isError;
    std::string text;
};

std::filesystem::path Directory();

bool WriteRequest(
    std::uint64_t sequence,
    const std::string& sessionId,
    const std::string& question,
    const std::string& context,
    std::string& error);

bool WriteCancellation(
    std::uint64_t sequence,
    const std::string& sessionId,
    std::string& error);

std::optional<Response> ReadResponse();
} // namespace RuptureCompanion::Bridge
